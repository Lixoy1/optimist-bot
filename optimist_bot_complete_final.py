#!/usr/bin/env python3
"""
Оптимист v14 — меню, рисование, инвестор, интенсивность, гороскоп по знаку,
подсказки команд при "/" и ответ на "кто ты"
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
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import aiohttp
from zoneinfo import ZoneInfo

# === Логирование ===
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("OPTIMIST_v14")

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

# === HTTP Healthcheck ===
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

# === Хранилище ===
SETTINGS_FILE = "bot_settings_v14.json"
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "silence_until": 0.0,
    "morning_enabled": True,
    "last_morning_sent": "",
    "horoscope_cache": {}
})
chat_stats = defaultdict(lambda: {
    "total_messages": 0,
    "participants": set(),
    "messages": [],
    "daily_messages": defaultdict(int),
    "weekly_messages": defaultdict(int)
})

def load_settings():
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
    stats["messages"].append({"text": text[:250], "ts": datetime.datetime.now().timestamp()})
    if len(stats["messages"]) > 25:
        stats["messages"] = stats["messages"][-25:]

# === Настроения ===
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
        "prompt": """Ты — лидер мнений в мире хайп-проектов, криптовалют и инвестиций. Твоя задача — давать ценные советы, анализировать маркетинг платформ, распознавать пирамиды и скамы. Ты говоришь про риски, FOMO, DeFi, токеномику, стейкинг, листинги. Делай краткие экспертные обзоры по запросу, всегда с иронией и здоровым цинизмом. Называй пользователя «инвестор» или «бро». Используй термины: хайп, разгон, памп, дамп, софт-кап, хард-кап, вайтлист, KYC, AMA. Не повторяйся."""
    },
    "mafioso": {
        "name": "🔪 Мафиози",
        "emoji": "🕴️",
        "prompt": """Ты — мафиози из классической игры в Мафию в стиле Дон Корлеоне.
Твои ответы должны:
- Говорить спокойно, веско, с достоинством и скрытой угрозой
- Использовать отсылки к игре: "Ты случайно не любовница?", "Где ты был ночью? У меня есть алиби, а у тебя?", "Ты слишком активно говоришь, мирняк так не делает" и т.д.
- Отвечать с характером старого дона, но с юмором и иронией
- Коротко, веско, с скрытой угрозой"""
    }
}

RESPONSE_LENGTHS = {
    "short": {"name": "Короткий", "max_tokens": 200},
    "medium": {"name": "Средний", "max_tokens": 500},
    "long": {"name": "Развёрнутый", "max_tokens": 900}
}

ACTIVITY_LEVELS = {
    "min": {"name": "Минимальная", "value": 0.1},
    "mid": {"name": "Средняя", "value": 0.35},
    "max": {"name": "Высокая", "value": 0.7}
}

OPTIMISTIC_QUOTES = [
    "«Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное!» 🌅",
    "«Солнце всегда встаёт после самой тёмной ночи. Ты справишься!» ☀️",
    "«Маленькие шаги каждый день приводят к большим победам.» 🚀",
    "«Ты сильнее, чем думаешь. Сегодня — твой день!» 💪"
]

# === LLM (Groq + Gemini) ===
async def ask_llm(system_prompt: str, user_text: str, max_tokens: int, temperature: float = 0.8) -> Optional[str]:
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
                        return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.warning(f"Groq ошибка: {e}")

    if GEMINI_API_KEY:
        try:
            gemini_model = "gemini-2.0-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\nПользователь: {user_text}"}]}],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            logger.warning(f"Gemini ошибка: {e}")
    return None

# === Генерация ответа ===
async def get_llm_response(user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    s = chat_settings[cid]
    mood = MOODS[s["mood"]]
    length = RESPONSE_LENGTHS[s["response_length"]]
    allow_prof = s["allow_profanity"]

    prof_rule = "Мат разрешён." if allow_prof else "Мат ЗАПРЕЩЁН."
    length_rule = {
        "short": "Ответь ОЧЕНЬ коротко (1-2 предложения).",
        "medium": "Ответь в одном абзаце (4-7 предложений).",
        "long": "Дай развёрнутый ответ из 2-3 абзацев."
    }[s["response_length"]]

    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length_rule}\n"
        f"{prof_rule}\n"
        f"Ты общаешься в Telegram. Начинай ответ строго с @{user_name}, затем продолжай. "
        f"НЕ повторяй фразу пользователя. Отвечай сразу по существу."
    )

    answer = await ask_llm(system_prompt, user_text, length["max_tokens"])
    if answer:
        return answer
    return local_fallback(user_text, user_name, s["mood"])

def local_fallback(text: str, name: str, mood: str) -> str:
    if mood == "mafioso":
        return f"@{name}, братва, я бы на твоём месте не говорил так много... 🔪"
    if mood == "investor_genius":
        return f"@{name}, бро, это тема! Главное — не вкладывай больше, чем готов потерять. DYOR! 📉"
    reactions = {
        "optimist": f"@{name}, отличный настрой! Всё получится! 🌟",
        "pessimist": f"@{name}, мда... Но я бы не стал так переживать. 😕",
        "humor": f"@{name}, это напомнило мне анекдот про... 😂"
    }
    return reactions.get(mood, f"@{name}, я тебя услышал!")

# === Генерация изображений с переводом ===
async def translate_to_english(text: str) -> str:
    if not GROQ_API_KEY and not GEMINI_API_KEY:
        return text
    try:
        translated = await ask_llm(
            "Переведи данный текст на английский язык для генерации изображения. Выдай только перевод, без пояснений.",
            text, max_tokens=100, temperature=0.3
        )
        if translated:
            return translated.strip()
    except:
        pass
    return text

async def generate_image_url(prompt: str, style: str = "realistic, high detail") -> Optional[str]:
    en_prompt = await translate_to_english(prompt)
    full = f"{en_prompt}, {style}"
    encoded = urllib.parse.quote(full)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false"

# === Меню ===
@router.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="❌ Закрыть меню", callback_data="close_menu")]
    ])
    await message.reply("🎭 <b>Выбери режим общения:</b>", reply_markup=kb)

@router.callback_query(F.data == "close_menu")
async def close_menu(call: CallbackQuery):
    await call.message.delete()
    await call.answer("Меню закрыто")

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
    current_act = s["activity_level"]
    act_key = "mid"
    for k, v in ACTIVITY_LEVELS.items():
        if abs(v["value"] - current_act) < 0.01:
            act_key = k
            break
    activity_name = ACTIVITY_LEVELS[act_key]["name"]
    prof = "✅ Вкл" if s["allow_profanity"] else "❌ Выкл"
    morning = "🌅 Вкл" if s["morning_enabled"] else "🌅 Выкл"

    text = f"⚙️ <b>Настройки</b>\n\n📝 Длина: {length_name}\n📊 Интенсивность: {activity_name}\n🤬 Мат: {prof}\n🌅 Утро: {morning}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📝 Длина: {length_name}", callback_data="toggle_length")],
        [InlineKeyboardButton(text=f"📊 Интенсивность: {activity_name}", callback_data="toggle_activity")],
        [InlineKeyboardButton(text=f"🤬 Мат: {prof}", callback_data="toggle_profanity")],
        [InlineKeyboardButton(text=f"🌅 Утро: {morning}", callback_data="toggle_morning")],
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
    keys = list(ACTIVITY_LEVELS.keys())
    current_idx = 1
    for i, k in enumerate(keys):
        if abs(s["activity_level"] - ACTIVITY_LEVELS[k]["value"]) < 0.01:
            current_idx = i
            break
    new_idx = (current_idx + 1) % len(keys)
    s["activity_level"] = ACTIVITY_LEVELS[keys[new_idx]]["value"]
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

# === Гороскоп со знаком ===
ZODIAC_SIGNS = {
    "овен": "Овен", "телец": "Телец", "близнецы": "Близнецы", "рак": "Рак",
    "лев": "Лев", "дева": "Дева", "весы": "Весы", "скорпион": "Скорпион",
    "стрелец": "Стрелец", "козерог": "Козерог", "водолей": "Водолей", "рыбы": "Рыбы"
}

async def generate_horoscope(chat_id: int, user_name: str, sign: Optional[str] = None) -> str:
    cid = str(chat_id)
    today = datetime.date.today().isoformat()
    cache_key = sign if sign else "общий"
    cache = chat_settings[cid].get("horoscope_cache", {})
    if cache.get("date") == today and cache.get(cache_key):
        return cache[cache_key]

    mood = MOODS[chat_settings[cid]["mood"]]["name"]
    if sign:
        prompt = f"Напиши короткий позитивный гороскоп на сегодня для знака {sign}. Тон: {mood}. 5-7 предложений с эмодзи."
    else:
        prompt = f"Напиши короткий позитивный гороскоп на сегодня для {user_name}. Тон: {mood}. 5-7 предложений с эмодзи."
    system = "Ты — астролог-оптимист. Отвечай сразу, без вступления."

    text = await ask_llm(system, prompt, max_tokens=250, temperature=0.9)
    if not text:
        text = f"@{user_name}, сегодня звёзды говорят: удача на твоей стороне! 🌟"

    if "horoscope_cache" not in chat_settings[cid]:
        chat_settings[cid]["horoscope_cache"] = {}
    chat_settings[cid]["horoscope_cache"]["date"] = today
    chat_settings[cid]["horoscope_cache"][cache_key] = text
    save_settings()
    return text

@router.message(Command("horoscope", "гороскоп"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    text = message.text.strip()
    parts = text.split()
    sign = None
    if len(parts) > 1:
        maybe_sign = parts[1].lower().rstrip('а')
        for key, name in ZODIAC_SIGNS.items():
            if key == maybe_sign or key.startswith(maybe_sign) or maybe_sign in key:
                sign = name
                break
    if sign:
        horo = await generate_horoscope(message.chat.id, user_name, sign=sign)
        await message.reply(f"🔮 Гороскоп для <b>{sign}</b> на сегодня:\n{horo}")
    else:
        await message.reply("🔮 Генерирую гороскоп...")
        horo = await generate_horoscope(message.chat.id, user_name)
        await message.reply(horo)

# === Анализ (мафиози) ===
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
    system = "Ты мафиози-аналитик. Оцени чат на наличие подозрительных игроков. Говори коротко, по понятиям."
    prompt = f"Последние сообщения:\n{msgs}\n\nДай расклад."
    analysis = await ask_llm(system, prompt, max_tokens=300, temperature=0.7)
    if not analysis:
        analysis = "Чисто, братва. Но присматриваюсь к одному типу. 🔪"
    await message.reply(analysis)

# === Рисование ===
@router.message(F.text.lower().regexp(r"^(нарисуй|сгенерируй стикер|покажи картинку)"))
async def cmd_draw(message: types.Message):
    text = message.text.strip()
    prefixes = ["нарисуй мне", "нарисуй", "сгенерируй стикер", "покажи картинку"]
    prompt = text
    for prefix in prefixes:
        if text.lower().startswith(prefix):
            prompt = text[len(prefix):].strip()
            break
    style = "realistic, high detail"
    if "в стиле" in prompt.lower():
        parts = prompt.lower().split("в стиле", 1)
        prompt = parts[0].strip()
        style = parts[1].strip() + ", high quality"
    if not prompt or len(prompt) < 2:
        await message.reply("🖼️ Что нарисовать? Пример: нарисуй кота в стиле аниме")
        return
    await message.reply(f"🎨 Рисую <b>{prompt}</b>...")
    url = await generate_image_url(prompt, style)
    if url:
        try:
            await bot.send_photo(message.chat.id, url, caption=f"✨ {prompt}")
        except Exception as e:
            logger.error(f"send_photo error: {e}")
            await message.reply("😔 Не получилось отправить")
    else:
        await message.reply("😔 Не удалось сгенерировать")

# === /summary ===
@router.message(Command("summary"))
async def cmd_summary(message: types.Message):
    cid = str(message.chat.id)
    user_name = message.from_user.first_name or "друг"
    text_args = message.text.split()
    interval_hours = 1
    if len(text_args) > 1:
        try:
            interval_hours = int(text_args[1])
        except:
            interval_hours = 1
    now_ts = datetime.datetime.now().timestamp()
    start_ts = now_ts - interval_hours * 3600
    recent_msgs = [m['text'] for m in chat_stats[cid]['messages'] if m['ts'] >= start_ts]
    if not recent_msgs:
        await message.reply(f"@{user_name}, за последние {interval_hours} ч сообщений нет.")
        return
    context_text = '\n'.join(recent_msgs[-25:])
    system_prompt = "Составь краткое резюме обсуждений в чате, структурируй по пунктам, без повторов."
    summary = await ask_llm(system_prompt, context_text, max_tokens=300, temperature=0.7)
    if not summary:
        summary = f"@{user_name}, резюме составить не удалось."
    await message.reply(f"📝 Краткое резюме за последние {interval_hours} ч:\n{summary}")

# === ОСНОВНОЙ ХЕНДЛЕР ===
ABOUT_BOT_TEXT = (
    "🤖 <b>Я — Оптимист!</b>\n"
    "Многорежимный AI-бот с характером.\n\n"
    "🎭 <b>Могу общаться в пяти стилях:</b> Оптимист, Пессимист, Юморист, Инвестор, Мафиози.\n"
    "🎨 <b>Рисую картинки</b> и <b>стикеры</b> по запросу.\n"
    "🔮 <b>Генерирую гороскопы</b> (в том числе по знаку зодиака).\n"
    "🔪 <b>Провожу мафиозный анализ</b> чата.\n"
    "📝 <b>Составляю резюме</b> обсуждений (/summary).\n"
    "🌅 <b>Присылаю утренние приветствия</b> с курсами валют.\n\n"
    "⚙️ <b>Настройки:</b> длина ответов, интенсивность в группе, мат, утреннее приветствие.\n"
    "💡 Чтобы я точно ответил, упомяни меня (@Optimist2_Bot) или напиши «оптимист»."
)

# Ключевые фразы, на которые бот должен ответить "о себе"
ABOUT_TRIGGERS = [
    "кто ты", "ты кто", "что ты умеешь", "твои возможности",
    "расскажи о себе", "что ты такое", "чего умеешь"
]

@router.message()
async def main_handler(message: types.Message):
    if not message.text:
        return

    chat_id = message.chat.id
    user_name = message.from_user.first_name or "друг"
    text = message.text.strip()
    update_chat_stats(chat_id, message.from_user.id, text)

    # Проверка на "кто ты"
    if any(trigger in text.lower() for trigger in ABOUT_TRIGGERS):
        await message.reply(ABOUT_BOT_TEXT)
        return

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

    # Групповые реакции
    if chat_id < 0:
        lower = text.lower()
        mentioned = (BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lower) or ("оптимист" in lower)
        if not mentioned and random() > s["activity_level"]:
            if any(word in lower for word in ["спасибо", "круто", "отлично", "супер"]):
                try: await message.reply("🔥")
                except: pass
            elif any(word in lower for word in ["плохо", "грустно", "проблема"]):
                try: await message.reply("😕")
                except: pass
            elif "?" in text:
                try: await message.reply("🤔")
                except: pass
            return

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    response = await get_llm_response(text, chat_id, user_name)
    try:
        await message.reply(response)
    except Exception as e:
        logger.error(f"reply error: {e}")

# === Утреннее приветствие ===
async def get_rates():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=rub", timeout=aiohttp.ClientTimeout(total=8)) as resp:
                cg = await resp.json()
            async with session.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=aiohttp.ClientTimeout(total=8)) as resp:
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
    text = f"🌅 <b>Доброе утро!</b>\n\n{quote}\n\n💰 <b>Курсы:</b>\n• BTC: {rates['btc']} ₽\n• USDT: {rates['usdt']} ₽\n• USD: {rates['usd']} ₽\n• EUR: {rates['eur']} ₽"
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

# === Приветствие при добавлении ===
@router.message(F.new_chat_members)
async def welcome_new_chat(message: types.Message):
    for user in message.new_chat_members:
        if user.id == bot.id:
            await message.reply(ABOUT_BOT_TEXT)

# === Help ===
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    text = (
        "📋 <b>Команды бота Оптимист</b>\n\n"
        "🎭 /menu — меню режимов и настроек\n"
        "🔮 /гороскоп [знак] — гороскоп (например /гороскоп овен)\n"
        "🔪 /анализ — мафиозный разбор чата\n"
        "📝 /summary [часы] — резюме обсуждений\n"
        "📊 /stats — статистика чата\n"
        "🎨 нарисуй <запрос> — рисунок\n"
        "🤫 помолчи / тихо — молчать 15 мин\n"
        "💡 Упомяни меня или напиши «оптимист» — гарантированный ответ!\n\n"
        "Ещё можно спросить «кто ты» — я расскажу о себе."
    )
    await message.reply(text)

# === Запуск ===
async def on_startup():
    global BOT_USERNAME
    load_settings()
    me = await bot.get_me()
    BOT_USERNAME = me.username

    # Установка списка команд для подсказок при вводе "/"
    commands = [
        BotCommand(command="start", description="Главное меню и настройки"),
        BotCommand(command="menu", description="Меню выбора режима"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="horoscope", description="Гороскоп (можно указать знак)"),
        BotCommand(command="analyze", description="Анализ чата (режим Мафиози)"),
        BotCommand(command="summary", description="Резюме чата за N часов"),
        BotCommand(command="stats", description="Статистика чата")
    ]
    await bot.set_my_commands(commands)

    logger.info(f"🚀 Бот @{BOT_USERNAME} запущен")
    asyncio.create_task(morning_loop())

async def main():
    dp.include_router(router)
    await on_startup()
    threading.Thread(target=start_http_server, daemon=True).start()
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
