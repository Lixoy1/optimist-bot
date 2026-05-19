#!/usr/bin/env python3
"""
Оптимист v7.1 — ПОЛНАЯ ВЕРСИЯ СО ВСЕМИ ФУНКЦИЯМИ + отладка Groq
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
logger = logging.getLogger("OPTIMIST_v7.1")
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    exit(1)
if not GROQ_API_KEY:
    logger.warning("⚠️ GROQ_API_KEY не задан — бот будет использовать заглушки")
bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
BOT_USERNAME: Optional[str] = None
# ==================== HTTP HEALTHCHECK (Railway) ====================
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
    def log_message(self, format, *args):
        pass
def start_http_server():
    PORT = int(os.environ.get("PORT", 8000))
    try:
        with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
            logger.info(f"🌐 HTTP health server on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server error: {e}")
# ==================== ХРАНИЛИЩЕ НАСТРОЕК ====================
SETTINGS_FILE = "bot_settings_v7.json"
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "silence_until": 0.0,
    "morning_enabled": True,
    "last_morning_sent": "",
    "horoscope_cache": {"date": "", "text": ""}
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
# ==================== НАСТРОЕНИЯ И ИНСТРУКЦИИ ====================
MOODS = {
    "optimist": {
        "name": "😊 Оптимист",
        "emoji": "🌟",
        "prompt": "Ты очень позитивный и мотивирующий бот. Всегда находи светлую сторону, поддерживай."
    },
    "pessimist": {
        "name": "😔 Пессимист",
        "emoji": "💀",
        "prompt": "Ты саркастичный пессимист с чёрным юмором. Предупреждай о рисках, но оставляй крошечную надежду."
    },
    "humor": {
        "name": "🤣 Юморист",
        "emoji": "😂",
        "prompt": "Ты профессиональный стендап-комик. Всё превращай в шутку, используй мемы и абсурд."
    },
    "investor_genius": {
        "name": "💰 Гений инвестиций",
        "emoji": "📈",
        "prompt": "Ты гений трейдинга и инвестиций с юмором. Говори про хайпы, риски, FOMO, используй сарказм."
    },
    "mafioso": {
        "name": "🔪 Мафиози",
        "emoji": "🕴️",
        "prompt": "Ты легендарный мафиози. Говори по понятиям, используй мафиозный жаргон: братва, развод, чисто."
    }
}
RESPONSE_LENGTHS = {
    "short": {"name": "Короткий", "max_tokens": 300, "instruction": "Ответь очень коротко (1-2 предложения)."},
    "medium": {"name": "Средний", "max_tokens": 600, "instruction": "Ответь средне (4-7 предложений)."},
    "long": {"name": "Развёрнутый", "max_tokens": 1000, "instruction": "Дай развёрнутый ответ (2-3 абзаца)."}
}
OPTIMISTIC_QUOTES = [
    "«Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное!» 🌅",
    "«Солнце всегда встаёт после самой тёмной ночи. Ты справишься!» ☀️",
    "«Маленькие шаги каждый день приводят к большим победам.» 🚀",
    "«Ты сильнее, чем думаешь. Сегодня — твой день!» 💪"
]
# ==================== СТАТИСТИКА ====================
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
# ==================== LLM (GROQ) ====================
async def get_llm_response(user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    s = chat_settings[cid]
    mood = MOODS.get(s["mood"], MOODS["optimist"])
    length = RESPONSE_LENGTHS.get(s["response_length"], RESPONSE_LENGTHS["medium"])
    allow_prof = s["allow_profanity"]
    # Контекст (последние сообщения)
    context_list = chat_stats[cid]["messages"][-12:]
    context = "\n".join([m["text"] for m in context_list]) if context_list else "Чат только начался."
    prof_instruction = "Можешь использовать мат (в меру)." if allow_prof else "СТРОГО без мата и грубых слов."
    system_prompt = f"""{mood['prompt']}
{length['instruction']}
{prof_instruction}
Ты в Telegram. ОБЯЗАТЕЛЬНО начинай ответ с @{user_name}, затем продолжай естественно.
Не повторяй контекст дословно.
Контекст: {context}"""
    payload = {
        "model": "llama-3.1-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.8,
        "max_tokens": length["max_tokens"]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
                else:
                    err = await resp.text()
                    logger.error(f"Groq HTTP {resp.status}: {err}")
                    logger.error(f"API Key (первые 10 символов): {GROQ_API_KEY[:10] if GROQ_API_KEY else 'None'}...")
    except Exception as e:
        logger.error(f"Groq error: {e}")
        logger.error(f"API Key (первые 10 символов): {GROQ_API_KEY[:10] if GROQ_API_KEY else 'None'}...")
    # Fallback
    return local_fallback(user_text, user_name, s["mood"])
def local_fallback(text: str, name: str, mood: str) -> str:
    if mood == "optimist":
        return f"@{name}, {text} — всё будет супер! 🌟"
    elif mood == "pessimist":
        return f"@{name}, {text} звучит тревожно, но может, пронесёт 😕"
    elif mood == "humor":
        return f"@{name}, {text} 😂 — это стендап-материал!"
    else:
        return f"@{name}, по поводу {text}... главное — без паники. 💰"
# ==================== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ====================
async def generate_image_url(prompt: str, style: str = "реализм") -> Optional[str]:
    try:
        full = f"{prompt}, {style}, high detail"
        encoded = urllib.parse.quote(full)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false"
    except Exception as e:
        logger.error(f"Pollinations error: {e}")
        return None
# ==================== МЕНЮ ====================
def create_main_menu(chat_id: int) -> InlineKeyboardMarkup:
    mood = chat_settings[str(chat_id)]["mood"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])
def create_settings_menu(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cid = str(chat_id)
    s = chat_settings[cid]
    length_name = RESPONSE_LENGTHS[s["response_length"]]["name"]
    prof = "✅ Вкл" if s["allow_profanity"] else "❌ Выкл"
    morning = "🌅 Вкл" if s["morning_enabled"] else "🌅 Выкл"
    text = (
        f"⚙️ <b>Настройки</b>\n\n"
        f"📝 Длина ответов: {length_name}\n"
        f"🤬 Мат: {prof}\n"
        f"🌅 Утреннее приветствие: {morning}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Длина: " + length_name, callback_data="toggle_length")],
        [InlineKeyboardButton(text="🤬 Мат: " + prof, callback_data="toggle_profanity")],
        [InlineKeyboardButton(text="🌅 Утро: " + morning, callback_data="toggle_morning")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    return text, kb
# ==================== ХЕНДЛЕРЫ ====================
@router.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    await message.reply("⚙️ <b>Выбери режим:</b>", reply_markup=create_main_menu(message.chat.id))
@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    cid = str(message.chat.id)
    stats = chat_stats[cid]
    total = stats["total_messages"]
    users = len(stats["participants"])
    today = datetime.date.today().isoformat()
    daily = stats["daily_messages"].get(today, 0)
    text = (
        f"📊 <b>Статистика чата</b>\n\n"
        f"Всего сообщений: <b>{total}</b>\n"
        f"Участников: <b>{users}</b>\n"
        f"Сегодня: <b>{daily}</b>"
    )
    await message.reply(text)
@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[str(call.message.chat.id)]["mood"] = mood_key
    save_settings()
    await call.answer(f"✅ {MOODS[mood_key]['name']}")
    await call.message.edit_text(f"Режим: {MOODS[mood_key]['name']}", reply_markup=create_main_menu(call.message.chat.id))
@router.callback_query(F.data == "settings")
async def show_settings(call: CallbackQuery):
    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb)
@router.callback_query(F.data == "toggle_length")
async def toggle_length(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    lengths = list(RESPONSE_LENGTHS.keys())
    current = lengths.index(s["response_length"])
    s["response_length"] = lengths[(current + 1) % len(lengths)]
    save_settings()
    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb)
@router.callback_query(F.data == "toggle_profanity")
async def toggle_profanity(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    s["allow_profanity"] = not s["allow_profanity"]
    save_settings()
    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb)
@router.callback_query(F.data == "toggle_morning")
async def toggle_morning(call: CallbackQuery):
    cid = str(call.message.chat.id)
    s = chat_settings[cid]
    s["morning_enabled"] = not s["morning_enabled"]
    save_settings()
    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb)
@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: CallbackQuery):
    await call.message.edit_text("⚙️ <b>Выбери режим:</b>", reply_markup=create_main_menu(call.message.chat.id))
# ==================== ГОРОСКОП (LLM) ====================
async def generate_horoscope(chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    today = datetime.date.today().isoformat()
    cache = chat_settings[cid].get("horoscope_cache", {"date": "", "text": ""})
    if cache.get("date") == today and cache.get("text"):
        return cache["text"]
    mood = MOODS.get(chat_settings[cid]["mood"], MOODS["optimist"])["name"]
    prompt = f"""Напиши короткий позитивный гороскоп на сегодня ({today}) для {user_name}.
Тон: {mood}. 5-7 предложений, с эмодзи."""
    try:
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "max_tokens": 250
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    horo = data["choices"][0]["message"]["content"].strip()
                    chat_settings[cid]["horoscope_cache"] = {"date": today, "text": horo}
                    save_settings()
                    return horo
    except Exception as e:
        logger.error(f"Horoscope error: {e}")
    return f"@{user_name}, сегодня звёзды на твоей стороне! 🌟"
@router.message(Command("horoscope", "гороскоп"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply("🔮 Генерирую гороскоп...")
    horo = await generate_horoscope(message.chat.id, user_name)
    await message.reply(horo)
# ==================== АНАЛИЗ ДЛЯ МАФИОЗИ ====================
@router.message(Command("analyze", "анализ"))
async def cmd_analyze(message: types.Message):
    if chat_settings[str(message.chat.id)]["mood"] != "mafioso":
        await message.reply("🔪 Аналитика только в режиме Мафиози. Переключи в /menu")
        return
    recent = chat_stats[str(message.chat.id)]["messages"][-15:]
    if not recent:
        await message.reply("Чат пуст, братва. Нечего анализировать.")
        return
    prompt = f"""Проанализируй последние сообщения чата как мафиози.
Сообщения: {chr(10).join([m['text'][:100] for m in recent])}
Дай короткий расклад: кто подозрительный, кого проверить."""
    try:
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 300
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=18)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    analysis = data["choices"][0]["message"]["content"].strip()
                    await message.reply(analysis)
                    return
    except Exception as e:
        logger.error(f"Analyze error: {e}")
    await message.reply("Братва, что-то с анализом не сложилось. Проверяй всех! 🔪")
# ==================== РИСОВАНИЕ ====================
@router.message(F.text.lower().regexp(r"^(нарисуй|сгенерируй стикер|покажи картинку)"))
async def cmd_draw(message: types.Message):
    text = message.text.lower()
    prompt = text
    for prefix in ["нарисуй", "сгенерируй стикер", "покажи картинку"]:
        if text.startswith(prefix):
            prompt = text[len(prefix):].strip()
            break
    if not prompt:
        await message.reply("🖼️ Что нарисовать? Например: нарисуй кота")
        return
    await message.reply(f"🎨 Рисую {prompt}...")
    url = await generate_image_url(prompt)
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
    # Групповые реакции
    if chat_id < 0: # группа
        lower_text = text.lower()
        mentioned = (BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lower_text) or ("оптимист" in lower_text)
        if not mentioned and random() > s["activity_level"]:
            if random() < 0.35:
                try:
                    await message.reply(choice(["👍", "🔥", "😊"]))
                except:
                    pass
            return
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    response = await get_llm_response(text, chat_id, user_name)
    try:
        await message.reply(response)
    except Exception as e:
        logger.error(f"reply error: {e}")
# ==================== УТРЕННЕЕ ПРИВЕТСТВИЕ (8:00 МСК) ====================
async def get_rates():
    try:
        async with aiohttp.ClientSession() as session:
            cg = await session.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=rub", timeout=aiohttp.ClientTimeout(total=8))
            cg_data = await cg.json()
            btc = round(cg_data["bitcoin"]["rub"])
            usdt = round(cg_data["tether"]["rub"])
            cbr = await session.get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=aiohttp.ClientTimeout(total=8))
            cbr_data = await cbr.json()
            usd = round(cbr_data["Valute"]["USD"]["Value"], 2)
            eur = round(cbr_data["Valute"]["EUR"]["Value"], 2)
            return {"btc": btc, "usdt": usdt, "usd": usd, "eur": eur}
    except Exception as e:
        logger.warning(f"Курсы не получены: {e}")
        return {"btc": "—", "usdt": "—", "usd": "—", "eur": "—"}
async def send_morning_greeting(chat_id: int):
    quote = choice(OPTIMISTIC_QUOTES)
    rates = await get_rates()
    text = (
        f"🌅 <b>Доброе утро!</b>\n\n{quote}\n\n"
        f"💰 Курсы:\nBTC: {rates['btc']} ₽\nUSDT: {rates['usdt']} ₽\nUSD: {rates['usd']} ₽\nEUR: {rates['eur']} ₽"
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
                    if s.get("morning_enabled", True) and s.get("last_morning_sent", "") != today:
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
# ==================== ЗАПУСК ====================
async def on_startup():
    global BOT_USERNAME
    load_settings()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"🚀 Бот @{BOT_USERNAME} запущен!")
    asyncio.create_task(morning_loop())
async def main():
    dp.include_router(router)
    await on_startup()
    threading.Thread(target=start_http_server, daemon=True).start()
    await dp.start_polling(bot, drop_pending_updates=True)
if __name__ == "__main__":
    asyncio.run(main())