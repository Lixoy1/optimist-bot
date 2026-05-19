import os
import json
import asyncio
import logging
import datetime
import urllib.parse
from collections import defaultdict
from random import choice, random

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from dotenv import load_dotenv
import aiohttp

# ====================== ЛОГИ ======================
logging.basicConfig(level=logging.INFO)
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

# ====================== НАСТРОЙКИ ======================
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "morning_enabled": True,
})

MOODS = {
    "optimist": {"name": "😊 Оптимист", "prompt": "Ты очень позитивный, энергичный и добрый бот."},
    "pessimist": {"name": "😔 Пессимист", "prompt": "Ты саркастичный пессимист с чёрным юмором."},
    "humor": {"name": "🤣 Юморист", "prompt": "Ты очень смешной стендап-комик."},
    "investor_genius": {"name": "💰 Гений инвестиций", "prompt": "Ты гений трейдинга и инвестиций."},
    "mafioso": {"name": "🔪 Мафиози", "prompt": "Ты крутой мафиози, говоришь по понятиям."}
}

# ====================== HEALTHCHECK ДЛЯ RAILWAY ======================
@router.get("/health")
async def health():
    return {"status": "ok", "bot": "running"}

# ====================== LLM ======================
async def get_llm_response(text: str, chat_id: int, user_name: str):
    mood = chat_settings[chat_id]["mood"]
    mood_prompt = MOODS.get(mood, MOODS["optimist"])["prompt"]

    system_prompt = f"""{mood_prompt}
Ты Telegram-бот «Оптимист».
Всегда начинай ответ с обращения: @{user_name},
Отвечай позитивно и по делу."""

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": "llama-3.1-70b-versatile",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": 600
                },
                timeout=20
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")

    return f"@{user_name}, я на связи! Чем могу помочь сегодня? 🌟"

# ====================== ОСНОВНОЙ ХЕНДЛЕР ======================
@router.message()
async def message_handler(message: types.Message):
    if not message.text:
        return

    user_name = message.from_user.first_name or "друг"
    response = await get_llm_response(message.text, message.chat.id, user_name)
    
    try:
        await message.reply(response)
    except:
        await message.reply("Я здесь! Чем помочь? 😊")

# ====================== КОМАНДЫ ======================
@router.message(Command("start", "menu"))
async def cmd_menu(message: types.Message):
    await message.reply("🌟 Привет! Я на связи.\nНапиши /menu для настроек.")

@router.message(Command("health"))
async def cmd_health(message: types.Message):
    await message.reply("✅ Бот работает нормально!")

# ====================== ЗАПУСК ======================
async def main():
    dp.include_router(router)
    logger.info("🚀 Бот успешно запущен на Railway!")
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
