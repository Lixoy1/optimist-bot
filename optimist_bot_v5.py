#!/usr/bin/env python3
"""
Оптимист v5 - РАБОЧАЯ ВЕРСИЯ ДЛЯ RAILWAY
"""

import os
import asyncio
import logging
import urllib.parse
from collections import defaultdict

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from dotenv import load_dotenv
import aiohttp
import http.server
import socketserver
import threading

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("OPTIMIST")

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    exit(1)

bot = Bot(token=TG_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# HTTP Server для healthcheck
class HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
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
    with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
        logger.info(f"HTTP server on port {PORT}")
        httpd.serve_forever()

# Настройки
chat_settings = defaultdict(lambda: {"mood": "optimist"})
MOODS = {
    "optimist": {"name": "Оптимист"},
    "pessimist": {"name": "Пессимист"},
    "humor": {"name": "Юморист"},
    "investor_genius": {"name": "Инвестор"},
    "mafioso": {"name": "Мафиози"}
}

async def generate_image_url(prompt):
    try:
        return f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=832&height=832&model=flux"
    except:
        return None

async def get_llm(user_text, chat_id, user_name):
    mood = MOODS.get(chat_settings[chat_id]["mood"], MOODS["optimist"])
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.1-70b-versatile",
                    "messages": [
                        {"role": "system", "content": f"Ты {mood['name']}. Начинай ответ с @{user_name},"},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": 600
                },
                timeout=25
            ) as resp:
                if resp.status == 200:
                    return (await resp.json())["choices"][0]["message"]["content"]
    except:
        pass
    return f"@{user_name}, я на связи!"

# Хендлеры
@router.message(Command("start"))
@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="Мафиози", callback_data="mood_mafioso")]
    ])
    await message.reply("Выбери режим:", reply_markup=kb)

@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: types.CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[call.message.chat.id]["mood"] = mood_key
    await call.answer(f"Режим: {MOODS[mood_key]['name']}")

@router.message(Command("гороскоп"))
@router.message(Command("horoscope"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply(f"@{user_name}, сегодня звёзды на твоей стороне! 🌟")

@router.message(Command("анализ"))
@router.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    if chat_settings[message.chat.id]["mood"] != "mafioso":
        await message.reply("Аналитика только в режиме Мафиози")
        return
    await message.reply("🕵️ Чисто, братва! Проверяй подозрительных! 🔪")

@router.message()
async def main_handler(message: types.Message):
    if not message.text:
        return
    user_name = message.from_user.first_name or "друг"
    text = message.text.lower()
    
    if text.startswith(("нарисуй", "сгенерируй стикер")):
        prompt = text.replace("нарисуй", "").replace("сгенерируй стикер", "").strip()
        if prompt:
            await message.reply(f"🎨 Рисую {prompt}...")
            url = await generate_image_url(prompt)
            if url:
                await bot.send_photo(message.chat.id, url, caption=prompt)
            else:
                await message.reply("Не получилось 😔")
        return
    
    response = await get_llm(message.text, message.chat.id, user_name)
    await message.reply(response)

# Регистрация
dp.include_router(router)

async def run_bot():
    logger.info("Telegram бот запускается...")
    await dp.start_polling(bot, drop_pending_updates=True)

def main():
    threading.Thread(target=start_http_server, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
