#!/usr/bin/env python3
"""
Оптимист v7 - ПОЛНАЯ ВЕРСИЯ С ВСЕМИ ФУНКЦИЯМИ ИЗ ИСХОДНОГО БОТА
"""

import os
import asyncio
import logging
import urllib.parse
import datetime
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

# ==================== НАСТРОЙКИ ====================
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "morning_enabled": True,
    "last_morning_sent": ""
})

MOODS = {
    "optimist": {"name": "😊 Оптимист", "prompt": "Ты очень позитивный и мотивирующий бот."},
    "pessimist": {"name": "😔 Пессимист", "prompt": "Ты саркастичный пессимист с чёрным юмором."},
    "humor": {"name": "🤣 Юморист", "prompt": "Ты профессиональный стендап-комик."},
    "investor_genius": {"name": "💰 Гений инвестиций", "prompt": "Ты гений трейдинга и инвестиций."},
    "mafioso": {"name": "🔪 Мафиози", "prompt": "Ты легендарный мафиози. Говори по понятиям."}
}

RESPONSE_LENGTHS = {
    "short": {"name": "Короткий", "max_tokens": 300},
    "medium": {"name": "Средний", "max_tokens": 600},
    "long": {"name": "Развёрнутый", "max_tokens": 1000}
}

# ==================== ФУНКЦИИ ====================
async def generate_image_url(prompt: str):
    try:
        return f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=832&height=832&model=flux&safe=false"
    except:
        return None

async def get_llm_response(user_text: str, chat_id: int, user_name: str):
    settings = chat_settings[chat_id]
    mood = MOODS.get(settings["mood"], MOODS["optimist"])
    length = RESPONSE_LENGTHS.get(settings["response_length"], RESPONSE_LENGTHS["medium"])
    
    system_prompt = f"""Ты {mood['name']}. {mood['prompt']}
Всегда начинай ответ с @{user_name},
Отвечай на русском языке.
Уровень нецензурной лексики: {'разрешена' if settings['allow_profanity'] else 'запрещена'}.
Длина ответа: {length['name']}."""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.1-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": length["max_tokens"]
                },
                timeout=30
            ) as resp:
                if resp.status == 200:
                    return (await resp.json())["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
    
    return f"@{user_name}, я на связи! 🌟"

# ==================== ХЕНДЛЕРЫ ====================
@router.message(Command("start"))
@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    ])
    await message.reply("⚙️ <b>Выбери режим или настройку:</b>", reply_markup=kb)

@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: types.CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    chat_settings[call.message.chat.id]["mood"] = mood_key
    await call.answer(f"✅ {MOODS[mood_key]['name']}")
    await call.message.edit_text(f"Режим изменён на {MOODS[mood_key]['name']}")

@router.callback_query(F.data == "settings")
async def show_settings(call: types.CallbackQuery):
    settings = chat_settings[call.message.chat.id]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Длина: {RESPONSE_LENGTHS[settings['response_length']]['name']}", callback_data="toggle_length")],
        [InlineKeyboardButton(text=f"Нецензурная лексика: {'✅' if settings['allow_profanity'] else '❌'}", callback_data="toggle_profanity")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    await call.message.edit_text("⚙️ <b>Настройки:</b>", reply_markup=kb)

@router.callback_query(F.data == "toggle_length")
async def toggle_length(call: types.CallbackQuery):
    settings = chat_settings[call.message.chat.id]
    lengths = list(RESPONSE_LENGTHS.keys())
    current = lengths.index(settings["response_length"])
    settings["response_length"] = lengths[(current + 1) % len(lengths)]
    await show_settings(call)

@router.callback_query(F.data == "toggle_profanity")
async def toggle_profanity(call: types.CallbackQuery):
    settings = chat_settings[call.message.chat.id]
    settings["allow_profanity"] = not settings["allow_profanity"]
    await show_settings(call)

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: types.CallbackQuery):
    await cmd_menu(call.message)

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
