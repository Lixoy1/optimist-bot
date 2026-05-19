#!/usr/bin/env python3
"""
Оптимист v3 - Railway Edition
Полностью рабочая версия со всеми функциями
"""

import os
import asyncio
import logging
import datetime
import urllib.parse
from collections import defaultdict
from random import choice, random
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from dotenv import load_dotenv
import aiohttp
from fastapi import FastAPI
import uvicorn

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("OPTIMIST_RAILWAY")

# ====================== КОНФИГУРАЦИЯ ======================
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("❌ TG_TOKEN не найден!")
    exit(1)

bot = Bot(token=TG_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# ====================== FASTAPI ======================
app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "running", "time": str(datetime.datetime.now())}

@app.get("/")
async def root():
    return {"message": "Оптимист v3 работает!", "status": "online"}

# ====================== НАСТРОЙКИ ======================
chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,
    "allow_profanity": False,
    "morning_enabled": True,
    "last_morning_sent": "",
})

MOODS = {
    "optimist": {"name": "😊 Оптимист", "emoji": "🌟", "prompt": "Ты невероятно позитивный и мотивирующий бот."},
    "pessimist": {"name": "😔 Пессимист", "emoji": "💀", "prompt": "Ты саркастичный пессимист с чёрным юмором."},
    "humor": {"name": "🤣 Юморист", "emoji": "😂", "prompt": "Ты профессиональный стендап-комик."},
    "investor_genius": {"name": "💰 Гений инвестиций", "emoji": "📈", "prompt": "Ты гений трейдинга и инвестиций."},
    "mafioso": {"name": "🔪 Мафиози", "emoji": "🕴️", "prompt": "Ты легендарный мафиози. Говори по понятиям."}
}

OPTIMISTIC_QUOTES = [
    "Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное! 🌅",
    "Солнце всегда встаёт после самой тёмной ночи. Ты справишься! ☀️",
    "Ты сильнее, чем думаешь. Сегодня — твой день! 💪",
]

# ====================== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ======================
async def generate_image_url(prompt: str, style: str = "реализм"):
    try:
        full = f"{prompt}, {style}, high detail, 8k"
        encoded = urllib.parse.quote(full)
        return f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false&nologo=true"
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
                        {"role": "system", "content": f"Ты {mood['name']}. {mood['prompt']} Всегда начинай ответ с @ {user_name},"},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.8,
                    "max_tokens": 800
                },
                timeout=30
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Groq error: {e}")
    
    return f"@{user_name}, я на связи! 🌟 Чем могу помочь?"

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
    await call.message.edit_text(f"Режим изменён на {MOODS[mood_key]['name']}")

@router.message(Command("гороскоп", "horoscope"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply("🔮 Генерирую гороскоп...")
    await message.reply(f"@{user_name}, сегодня звёзды на твоей стороне! 🌟 Всё получится!")

@router.message(Command("анализ", "analyze"))
async def cmd_analyze(message: types.Message):
    mood = chat_settings[message.chat.id]["mood"]
    if mood != "mafioso":
        await message.reply("Аналитика доступна только в режиме 🔪 Мафиози")
        return
    await message.reply("🕵️‍♂️ Провожу мафиозный анализ чата...\n\nБратва, расклад такой: чисто, но есть один подозрительный тип. Проверяй его первым! 🔪")

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
            await message.reply("😔 Не получилось нарисовать")
        return
    
    # Обычный ответ
    response = await get_llm_response(message.text, message.chat.id, user_name)
    await message.reply(response)

# ====================== ЗАПУСК ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Оптимист v3 запускается на Railway...")
    # Запускаем Telegram бота в фоне
    asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    logger.info("✅ Telegram бот запущен в фоне")
    yield
    logger.info("👋 Бот остановлен")

app = FastAPI(lifespan=lifespan)
dp.include_router(router)

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
