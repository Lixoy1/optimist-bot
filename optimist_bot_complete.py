#!/usr/bin/env python3
"""
Оптимист v6 - ПОЛНАЯ ВЕРСИЯ С ВСЕМИ ФУНКЦИЯМИ
"""

import os
import asyncio
import logging
import urllib.parse
from collections import defaultdict
import threading
import http.server
import socketserver

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
import aiohttp

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("OPTIMIST")

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    exit(1)

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# HTTP Server
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
    def log_message(self, format, *args): pass

def start_http_server():
    PORT = int(os.environ.get("PORT", 8000))
    with socketserver.TCPServer(("", PORT), HealthHandler) as httpd:
        logger.info(f"HTTP server on port {PORT}")
        httpd.serve_forever()

# Настройки
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "allow_profanity": False
})

MOODS = {
    "optimist": {"name": "😊 Оптимист", "prompt": "Ты очень позитивный и мотивирующий бот."},
    "pessimist": {"name": "😔 Пессимист", "prompt": "Ты саркастичный пессимист с чёрным юмором."},
    "humor": {"name": "🤣 Юморист", "prompt": "Ты профессиональный стендап-комик."},
    "investor_genius": {"name": "💰 Гений инвестиций", "prompt": "Ты гений трейдинга и инвестиций."},
    "mafioso": {"name": "🔪 Мафиози", "prompt": "Ты легендарный мафиози. Говори по понятиям."}
}

async def generate_image_url(prompt: str):
    try:
        return f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=832&height=832&model=flux&safe=false"
    except:
        return None

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
                        {"role": "system", "content": f"Ты {mood['name']}. {mood['prompt']} Всегда начинай ответ с @{user_name},"},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": 700
                },
                timeout=30
            ) as resp:
                if resp.status == 200:
                    return (await resp.json())["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
    
    return f"@{user_name}, я на связи! 🌟"

# Хендлеры
@router.message(Command("start"))
@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")]
    ])
    await message.reply("⚙️ <b>Выбери режим:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: types.CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[call.message.chat.id]["mood"] = mood_key
    await call.answer(f"✅ {MOODS[mood_key]['name']}")
    await call.message.edit_text(f"Режим изменён на {MOODS[mood_key]['name']}")

@router.message(Command("гороскоп"))
@router.message(Command("horoscope"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply(f"@{user_name}, сегодня звёзды на твоей стороне! 🌟 Всё получится!")

@router.message(Command("анализ"))
@router.message(Command("analyze"))
async def cmd_analyze(message: types.Message):
    if chat_settings[message.chat.id]["mood"] != "mafioso":
        await message.reply("Аналитика только в режиме 🔪 Мафиози")
        return
    await message.reply("🕵️‍♂️ Чисто, братва! Но есть один подозрительный. Проверяй его первым! 🔪")

@router.message()
async def main_handler(message: types.Message):
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
    
    # Обычный LLM ответ
    response = await get_llm_response(message.text, message.chat.id, user_name)
    await message.reply(response)

dp.include_router(router)

async def run_bot():
    logger.info("Telegram бот запускается...")
    await dp.start_polling(bot, drop_pending_updates=True)

def main():
    threading.Thread(target=start_http_server, daemon=True).start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
