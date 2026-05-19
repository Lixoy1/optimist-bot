#!/usr/bin/env python3
"""
Оптимист v9 — ФИНАЛЬНАЯ ВЕРСИЯ
Полный бот со всеми функциями из описания
"""

import os
import json
import asyncio
import logging
import urllib.parse
import datetime
from collections import defaultdict
from random import choice, random
from typing import Optional
import threading
import http.server
import socketserver

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import aiohttp
from zoneinfo import ZoneInfo

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("OPTIMIST_v9")

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    exit(1)

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
BOT_USERNAME: Optional[str] = None

# ==================== HTTP HEALTHCHECK ====================
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args): pass

def start_http_server():
    PORT = int(os.environ.get("PORT", 8000))
    try:
        with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
            logger.info(f"🌐 HTTP health server on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server error: {e}")

# ==================== НАСТРОЙКИ ====================
SETTINGS_FILE = "bot_settings_v9.json"
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "silence_until": 0.0,
    "morning_enabled": True,
    "last_morning_sent": "",
    "horoscope_cache": {"date": "", "text": ""},
    "context_reactions": True
})

chat_stats = defaultdict(lambda: {
    "total_messages": 0,
    "participants": set(),
    "messages": [],
    "daily_messages": defaultdict(int),
    "weekly_messages": defaultdict(int)
})

def load_settings():
    global chat_settings, chat_stats
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for cid, s in data.get("chat_settings", {}).items():
                chat_settings[cid].update(s)
            for cid, s in data.get("chat_stats", {}).items():
                chat_stats[cid].update({
                    "total_messages": s.get("total_messages", 0),
                    "participants": set(s.get("participants", [])),
                    "messages": s.get("messages", []),
                    "daily_messages": defaultdict(int, s.get("daily_messages", {})),
                    "weekly_messages": defaultdict(int, s.get("weekly_messages", {}))
                })
        logger.info("✅ Настройки загружены")
    except FileNotFoundError:
        logger.info("📁 Новый файл настроек создан")

def save_settings():
    data = {
        "chat_settings": {k: dict(v) for k, v in chat_settings.items()},
        "chat_stats": {
            k: {
                "total_messages": v["total_messages"],
                "participants": list(v["participants"]),
                "messages": v["messages"][-25:],
                "daily_messages": dict(v["daily_messages"]),
                "weekly_messages": dict(v["weekly_messages"])
            } for k, v in chat_stats.items()
        }
    }
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def update_chat_stats(chat_id: int, user_id: int, text: str):
    cid = str(chat_id)
    stats = chat_stats[cid]
    today = datetime.date.today().isoformat()
    week = str(datetime.datetime.now().isocalendar()[1])
    stats["total_messages"] += 1
    stats["participants"].add(user_id)
    stats["daily_messages"][today] += 1
    stats["weekly_messages"][week] += 1
    stats["messages"].append({
        "text": text[:250],
        "ts": datetime.datetime.now().timestamp()
    })
    if len(stats["messages"]) > 25:
        stats["messages"] = stats["messages"][-25:]

# ==================== НАСТРОЕНИЯ ====================
MOODS = {
    "optimist": {
        "name": "😊 Оптимист",
        "emoji": "🌟",
        "prompt": "Ты — жизнерадостный оптимист. Поддерживаешь, вдохновляешь. Никогда не повторяй вопрос пользователя в ответе."
    },
    "pessimist": {
        "name": "😔 Пессимист",
        "emoji": "💀",
        "prompt": "Ты — саркастичный пессимист с чёрным юмором. Честно предупреждаешь о рисках, но не токсично."
    },
    "humor": {
        "name": "🤣 Юморист",
        "emoji": "😂",
        "prompt": "Ты — стендап-комик. Отвечаешь шутками и мемами. Не повторяешь запрос."
    },
    "investor_genius": {
        "name": "💰 Гений инвестиций",
        "emoji": "📈",
        "prompt": "Ты — гений трейдинга. Говоришь про хайпы, риски, FOMO с юмором и сарказмом."
    },
    "mafioso": {
        "name": "🔪 Мафиози",
        "emoji": "🕴️",
        "prompt": "Ты — авторитетный мафиози в стиле Дон Корлеоне. Говоришь по понятиям, используешь жаргон: братва, развод, чисто, по фэншую."
    }
}

RESPONSE_LENGTHS = {
    "short": {"name": "Короткий", "max_tokens": 200},
    "medium": {"name": "Средний", "max_tokens": 500},
    "long": {"name": "Развёрнутый", "max_tokens": 900}
}

ACTIVITY_LEVELS = [0.1, 0.25, 0.5, 0.75, 1.0]
ACTIVITY_NAMES = ["Очень мало", "Мало", "Средняя", "Высокая", "Максимум"]

OPTIMISTIC_QUOTES = [
    "«Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное!» 🌅",
    "«Солнце всегда встаёт после самой тёмной ночи. Ты справишься!» ☀️",
    "«Маленькие шаги каждый день приводят к большим победам.» 🚀",
    "«Ты сильнее, чем думаешь. Сегодня — твой день!» 💪"
]

# ==================== LLM ====================
async def ask_llm(system_prompt: str, user_text: str, max_tokens: int, temperature: float = 0.8) -> Optional[str]:
    """Пытается получить ответ сначала от Groq, потом от Gemini"""
    # 1. Groq
    if GROQ_API_KEY:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_text}
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info("✅ Ответ от Groq получен")
                        return data["choices"][0]["message"]["content"].strip()
                    else:
                        logger.warning(f"Groq вернул {resp.status}")
        except Exception as e:
            logger.warning(f"Groq ошибка: {e}")
    
    # 2. Gemini (если есть ключ и Groq не сработал)
    if GEMINI_API_KEY:
        try:
            gemini_model = "gemini-2.0-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{system_prompt}\n\nПользователь: {user_text}"}]}
                ],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info("✅ Ответ от Gemini получен")
                        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    else:
                        logger.warning(f"Gemini вернул {resp.status}")
        except Exception as e:
            logger.warning(f"Gemini ошибка: {e}")
    
    return None

# ==================== ГЕНЕРАЦИЯ ОТВЕТА ====================
async def get_llm_response(user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    s = chat_settings[cid]
    mood = MOODS[s["mood"]]
    length_key = s["response_length"]
    length = RESPONSE_LENGTHS[length_key]
    allow_prof = s["allow_profanity"]
    
    prof_rule = "Мат разрешён." if allow_prof else "Мат ЗАПРЕЩЁН."
    length_rule = {
        "short": "Ответь ОЧЕНЬ коротко (1-2 предложения).",
        "medium": "Ответь в одном абзаце (4-7 предложений).",
        "long": "Дай развёрнутый ответ из 2-3 абзацев."
    }[length_key]
    
    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length_rule}\n"
        f"{prof_rule}\n"
        f"Ты общаешься в Telegram. Начинай ответ строго с @{user_name}, затем продолжай. "
        f"НЕ повторяй фразу пользователя. Не пиши «ты спросил...» или «по поводу...». "
        f"Отвечай сразу по существу, живо и эмоционально."
    )
    
    # Попытка через LLM
    answer = await ask_llm(system_prompt, user_text, length["max_tokens"])
    if answer:
        return answer
    
    # Если LLM не ответила — креативный fallback
    logger.warning("LLM не ответила, использую fallback")
    return local_fallback(user_text, user_name, s["mood"])

def local_fallback(text: str, name: str, mood: str) -> str:
    reactions = {
        "optimist": f"@{name}, отличный настрой! Всё получится! 🌟",
        "pessimist": f"@{name}, мда... Но я бы не стал так переживать. 😕",
        "humor": f"@{name}, это напомнило мне анекдот про... 😂",
        "investor_genius": f"@{name}, с такими мыслями только в крипту! 💰",
        "mafioso": f"@{name}, братва, разберёмся. Чисто по фэншую. 🔪"
    }
    return reactions.get(mood, f"@{name}, я тебя услышал!")

# ==================== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ====================
async def generate_image_url(prompt: str, style: str = "реализм, высокая детализация") -> Optional[str]:
    try:
        full = f"{prompt}, {style}"
        encoded = urllib.parse.quote(full)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false"
    except Exception as e:
        logger.error(f"Pollinations error: {e}")
        return None

# ==================== МЕНЮ (с исчезанием) ====================
@router.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])
    await message.reply("⚙️ <b>Выбери режим:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[str(call.message.chat.id)]["mood"] = mood_key
    save_settings()
    await call.message.edit_text(f"✅ Режим изменён на {MOODS[mood_key]['name']}", reply_markup=None)
    await call.answer()

@router.callback_query(F.data == "settings")
async def show_settings(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    length_name = RESPONSE_LENGTHS[s["response_length"]]["name"]
    activity_name = ACTIVITY_NAMES[ACTIVITY_LEVELS.index(s["activity_level"])] if s["activity_level"] in ACTIVITY_LEVELS else "Средняя"
    prof = "✅ Вкл" if s["allow_profanity"] else "❌ Выкл"
    morning = "🌅 Вкл" if s["morning_enabled"] else "🌅 Выкл"
    
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"📝 Длина ответов: {length_name}\n"
        f"📊 Интенсивность: {activity_name}\n"
        f"🤬 Мат: {prof}\n"
        f"🌅 Утро: {morning}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Длина: " + length_name, callback_data="toggle_length")],
        [InlineKeyboardButton(text="📊 Интенсивность: " + activity_name, callback_data="toggle_activity")],
        [InlineKeyboardButton(text="🤬 Мат: " + prof, callback_data="toggle_profanity")],
        [InlineKeyboardButton(text="🌅 Утро: " + morning, callback_data="toggle_morning")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "toggle_length")
async def toggle_length(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    lengths = list(RESPONSE_LENGTHS.keys())
    current = lengths.index(s["response_length"])
    s["response_length"] = lengths[(current + 1) % len(lengths)]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_activity")
async def toggle_activity(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    current = ACTIVITY_LEVELS.index(s["activity_level"]) if s["activity_level"] in ACTIVITY_LEVELS else 2
    s["activity_level"] = ACTIVITY_LEVELS[(current + 1) % len(ACTIVITY_LEVELS)]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_profanity")
async def toggle_profanity(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    s["allow_profanity"] = not s["allow_profanity"]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_morning")
async def toggle_morning(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    s["morning_enabled"] = not s["morning_enabled"]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: CallbackQuery):
    await cmd_menu(call.message)

# ==================== ГОРОСКОП ====================
async def generate_horoscope(chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    today = datetime.date.today().isoformat()
    cache = chat_settings[cid].get("horoscope_cache", {})
    if cache.get("date") == today and cache.get("text"):
        return cache["text"]
    
    mood = MOODS[chat_settings[cid]["mood"]]["name"]
    prompt = f"Напиши короткий позитивный гороскоп на сегодня для {user_name}. Тон: {mood}. 5-7 предложений с эмодзи."
    system = "Ты — астролог-оптимист. Отвечай сразу, без вступления."
    
    text = await ask_llm(system, prompt, max_tokens=250, temperature=0.9)
    if not text:
        text = f"@{user_name}, сегодня звёзды говорят: удача на твоей стороне! 🌟"
    
    chat_settings[cid]["horoscope_cache"] = {"date": today, "text": text}
    save_settings()
    return text

@router.message(Command("horoscope", "гороскоп"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply("🔮 Генерирую гороскоп...")
    horo = await generate_horoscope(message.chat.id, user_name)
    await message.reply(horo)

# ==================== АНАЛИЗ ДЛЯ МАФИОЗИ ====================
@router.message(Command("analyze", "анализ"))
async def cmd_analyze(message: types.Message):
    cid = str(message.chat.id)
    if chat_settings[cid]["mood"] != "mafioso":
        await message.reply("🔪 Аналитика только в режиме Мафиози. Переключи в /menu")
        return
    
    recent = chat_stats[cid]["messages"][-15:]
    if not recent:
        await message.reply("Чат пуст, братва.")
        return
    
    msgs = "\n".join([m["text"][:100] for m in recent])
    system = "Ты мафиози-аналитик в стиле Дон Корлеоне. Оцени чат на наличие подозрительных игроков. Говори коротко, по понятиям."
    prompt = f"Последние сообщения в чате:\n{msgs}\nДай расклад."
    
    analysis = await ask_llm(system, prompt, max_tokens=300, temperature=0.7)
    if not analysis:
        analysis = "Чисто, братва. Но присматриваюсь к одному типу. 🔪"
    
    await message.reply(analysis)

# ==================== РИСОВАНИЕ ====================
@router.message(F.text.lower().regexp(r"^(нарисуй|сгенерируй стикер|покажи картинку)"))
async def cmd_draw(message: types.Message):
    text = message.text.strip()
    prefixes = ["нарисуй мне", "нарисуй", "сгенерируй стикер", "покажи картинку"]
    prompt = text
    for prefix in prefixes:
        if text.lower().startswith(prefix):
            prompt = text[len(prefix):].strip()
            break
    
    if not prompt or len(prompt) < 2:
        await message.reply("🖼️ Что нарисовать? Пример: нарисуй кота в стиле аниме")
        return
    
    await message.reply(f"🎨 Рисую <b>{prompt}</b>...")
    url = await generate_image_url(prompt, "реализм, высокая детализация")
    
    if url:
        try:
            await bot.send_photo(message.chat.id, url, caption=f"✨ {prompt}")
        except Exception as e:
            logger.error(f"send_photo error: {e}")
            await message.reply("😔 Не получилось отправить")
    else:
        await message.reply("😔 Не удалось сгенерировать")

# ==================== ОСНОВНОЙ ХЕНДЛЕР ====================
@router.message()
async def main_handler(message: types.Message):
    if not message.text:
        return
    
    chat_id = message.chat.id
    user_name = message.from_user.first_name or "друг"
    text = message.text.strip()
    
    update_chat_stats(chat_id, message.from_user.id, text)
    cid = str(chat_id)
    s = chat_settings[cid]
    
    # Тишина
    if s["silence_until"] > datetime.datetime.now().timestamp():
        return
    
    if any(word in text.lower() for word in ["помолчи", "тихо"]):
        s["silence_until"] = datetime.datetime.now().timestamp() + 900
        save_settings()
        await message.reply("🤫 Молчу 15 минут.")
        return
    
    # Групповые реакции (по контексту)
    if chat_id < 0:
        lower = text.lower()
        mentioned = (BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lower) or ("оптимист" in lower)
        
        if not mentioned and random() > s["activity_level"]:
            # Реакции по контексту
            if any(word in lower for word in ["спасибо", "круто", "отлично", "супер"]):
                try:
                    await message.reply("🔥")
                except:
                    pass
            elif any(word in lower for word in ["плохо", "грустно", "проблема"]):
                try:
                    await message.reply("😕")
                except:
                    pass
            elif "?" in text:
                try:
                    await message.reply("🤔")
                except:
                    pass
            return
    
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    response = await get_llm_response(text, chat_id, user_name)
    
    try:
        await message.reply(response)
    except Exception as e:
        logger.error(f"reply error: {e}")

# ==================== УТРЕННЕЕ ПРИВЕТСТВИЕ ====================
async def get_rates():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=rub",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                cg = await resp.json()
            async with session.get(
                "https://www.cbr-xml-daily.ru/daily_json.js",
                timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                cbr = await resp.json()
            return {
                "btc": round(cg["bitcoin"]["rub"]),
                "usdt": round(cg["tether"]["rub"]),
                "usd": round(cbr["Valute"]["USD"]["Value"], 2),
                "eur": round(cbr["Valute"]["EUR"]["Value"], 2)
            }
    except Exception as e:
        logger.warning(f"Курсы не получены: {e}")
        return {"btc": "—", "usdt": "—", "usd": "—", "eur": "—"}

async def send_morning_greeting(chat_id: int):
    quote = choice(OPTIMISTIC_QUOTES)
    rates = await get_rates()
    text = (
        f"🌅 <b>Доброе утро!</b>\n\n{quote}\n\n"
        f"💰 <b>Курсы:</b>\n"
        f"• BTC: {rates['btc']} ₽\n"
        f"• USDT: {rates['usdt']} ₽\n"
        f"• USD: {rates['usd']} ₽\n"
        f"• EUR: {rates['eur']} ₽"
    )
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"Утреннее сообщение ошибка: {e}")

async def morning_loop():
    msk = ZoneInfo("Europe/Moscow")
    while True:
        try:
            now = datetime.datetime.now(msk)
            if now.hour == 8 and now.minute < 5:
                today = now.date().isoformat()
                for cid_str, s in list(chat_settings.items()):
                    if s.get("morning_enabled") and s.get("last_morning_sent") != today:
                        try:
                            await send_morning_greeting(int(cid_str))
                            chat_settings[cid_str]["last_morning_sent"] = today
                            save_settings()
                        except Exception as e:
                            logger.error(f"Утро для {cid_str}: {e}")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Morning loop error: {e}")
            await asyncio.sleep(60)

# ==================== ПРИВЕТСТВИЕ ПРИ ДОБАВЛЕНИИ В ЧАТ ====================
@router.message(F.new_chat_members)
async def welcome_new_chat(message: types.Message):
    for user in message.new_chat_members:
        if user.id == bot.id:
            text = (
                f"🤖 <b>Привет! Я Оптимист</b>\n\n"
                f"Я — многорежимный AI-бот с характером. Умею:\n"
                f"• Отвечать умно в 5 разных стилях\n"
                f"• Рисовать картинки и стикеры\n"
                f"• Давать гороскопы\n"
                f"• Проводить мафиозную аналитику чата\n"
                f"• Присылать утренние приветствия с курсами\n\n"
                f"Напиши /menu чтобы выбрать режим и настроить меня под себя!\n\n"
                f"По умолчанию я в режиме Оптимист 🌟"
            )
            await message.reply(text)
            break

# ==================== HELP ====================
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📋 <b>Команды бота Оптимист</b>\n\n"
        "🎭 <b>Режимы:</b>\n"
        "/menu — открыть меню выбора режима и настроек\n\n"
        "🔮 <b>Гороскоп:</b>\n"
        "/гороскоп или /horoscope — персональный гороскоп на сегодня\n\n"
        "🔪 <b>Мафиозная аналитика:</b>\n"
        "/анализ или /analyze — разбор чата (только в режиме Мафиози)\n\n"
        "📊 <b>Статистика:</b>\n"
        "/stats — статистика чата\n\n"
        "🎨 <b>Рисование:</b>\n"
        "нарисуй <запрос> — создать картинку\n"
        "сгенерируй стикер <запрос> — создать стикер\n"
        "покажи картинку <запрос> — показать картинку\n\n"
        "🤫 <b>Тишина:</b>\n"
        "помолчи / тихо / молчи 15 — бот замолчит на 15 минут\n\n"
        "⚙️ <b>Настройки (в /menu):</b>\n"
        "• Длина ответов (коротко/средне/развёрнуто)\n"
        "• Интенсивность в группе (как часто реагирует)\n"
        "• Разрешить/запретить мат\n"
        "• Утреннее приветствие (вкл/выкл)\n\n"
        "💡 <b>Совет:</b> Упомяни меня (@Optimist2_Bot) или напиши «оптимист», чтобы я гарантированно ответил!"
    )
    await message.reply(text)

# ==================== ЗАПУСК ====================
async def on_startup():
    global BOT_USERNAME
    load_settings()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"🚀 Бот @{BOT_USERNAME} запущен")
    asyncio.create_task(morning_loop())

async def main():
    dp.include_router(router)
    await on_startup()
    threading.Thread(target=start_http_server, daemon=True).start()
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
