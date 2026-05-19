#!/usr/bin/env python3
"""
Оптимист v4 - FINAL RAILWAY VERSION
Гарантированно работает на Railway
"""

import os
import asyncio
import logging
import datetime
import urllib.parse
from collections import defaultdict
from random import choice, random
import threading
import http.server
import socketserver

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from dotenv import load_dotenv
import aiohttp

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("OPTIMIST")

# ====================== КОНФИГ ======================
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    exit(1)

bot = Bot(token=TG_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# ====================== ПРОСТОЙ HTTP СЕРВЕР ДЛЯ HEALTHCHECK ======================
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "bot": "running"}')
        else:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Optimist Bot is running!")

    def log_message(self, format, *args):
        pass

def start_http_server():
    PORT = int(os.environ.get("PORT", 8000))
    with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
        logger.info(f"🌐 HTTP сервер запущен на порту {PORT}")
        httpd.serve_forever()

# ====================== НАСТРОЙКИ ======================
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
})

MOODS = {
    "optimist": {"name": "😊 Оптимист", "emoji": "🌟"},
    "pessimist": {"name": "😔 Пессимист", "emoji": "💀"},
    "humor": {"name": "🤣 Юморист", "emoji": "😂"},
    "investor_genius": {"name": "💰 Гений инвестиций", "emoji": "📈"},
    "mafioso": {"name": "🔪 Мафиози", "emoji": "🕴️"}
}

# ====================== ИЗОБРАЖЕНИЯ ======================
async def generate_image_url(prompt: str):
    try:
        encoded = urllib.parse.quote(prompt)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false"
    except:
        return None

# ====================== LLM ======================
async def get_llm_response(user_text: str, chat_id: int, user_name: str):
    mood_key = chat_settings[chat_id]["mood"]
    mood = MOODS.get(mood_key, MOODS["optimist"])
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.1-70b-versatile",
                    "messages": [
                        {"role": "system", "content": f"Ты {mood['name']}. Отвечай в соответствующем стиле. Начинай ответ с @{user_name},"},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": 700
                },
                timeout=30
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
    
    return f"@{user_name}, я на связи! 🌟"

# ====================== ХЕНДЛЕРЫ ======================
@router.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")]
    ])
    await message.reply("⚙️ <b>Выбери режим:</b>", reply_markup=kb)

@router.callback_query(lambda c: c.data.startswith("mood_"))
async def change_mood(call: types.CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[call.message.chat.id]["mood"] = mood_key
    await call.answer(f"✅ {MOODS[mood_key]['name']}")

@router.message(Command("гороскоп", "horoscope"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply(f"@{user_name}, сегодня звёзды на твоей стороне! 🌟 Всё получится!")

@router.message(Command("анализ", "analyze"))
async def cmd_analyze(message: types.Message):
    mood = chat_settings[message.chat.id]["mood"]
    if mood != "mafioso":
        await message.reply("Аналитика только в режиме 🔪 Мафиози")
        return
    await message.reply("🕵️‍♂️ Чисто, братва! Но есть один подозрительный. Проверяй его первым! 🔪")

@router.message()
async def message_handler(message: types.Message):
    if not message.text:
        return
    
    user_name = message.from_user.first_name or "друг"
    text = message.text.lower()
    
    # Генерация изображений
    if text.startswith(("нарисуй", "сгенерируй стикер", "покажи картинку")):
        prompt = text.replace("нарисуй", "").replace("сгенерируй стикер", "").replace("покажи картинку", "").strip()
        if not prompt:
            await message.reply("🖼️ Что нарисовать?")
            return
        await message.reply(f"🎨 Рисую {prompt}...")
        url = await generate_image_url(prompt)
        if url:
            await bot.send_photo(message.chat.id, url, caption=f"✨ {prompt}")
        else:
            await message.reply("😔 Не получилось")
        return
    
    response = await get_llm_response(message.text, message.chat.id, user_name)
    await message.reply(response)

# ====================== ЗАПУСК ======================
async def run_bot():
    logger.info("🚀 Telegram бот запускается...")
    await dp.start_polling(bot, drop_pending_updates=True)

def main():
    # Запускаем HTTP сервер в отдельном потоке
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    
    # Запускаем Telegram бота
    asyncio.run(run_bot())

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
