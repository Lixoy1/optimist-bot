import os
import asyncio
import logging
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from fastapi import FastAPI
import uvicorn
from contextlib import asynccontextmanager

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("OPTIMIST")

# ====================== КОНФИГУРАЦИЯ ======================
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("❌ TG_TOKEN не найден в переменных окружения!")
    exit(1)

bot = Bot(token=TG_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

# ====================== FASTAPI ДЛЯ HEALTHCHECK ======================
app = FastAPI()

@app.get("/health")
async def health():
    return {"status": "ok", "bot": "running"}

# ====================== ОСНОВНАЯ ЛОГИКА БОТА ======================
@router.message()
async def message_handler(message: types.Message):
    if not message.text:
        return

    user_name = message.from_user.first_name or "друг"
    text = message.text.lower()

    if text in ["/start", "/menu", "/help"]:
        await message.reply(
            "🌟 <b>Привет! Я Оптимист v2</b>\n\n"
            "Я могу:\n"
            "• Отвечать на любые вопросы\n"
            "• Рисовать картинки (напиши «нарисуй ...»)\n"
            "• Делать гороскоп (/гороскоп)\n"
            "• Анализировать чат в режиме Мафиози\n\n"
            "Выбери настроение через /menu 😊"
        )
    else:
        # Простой ответ пока
        await message.reply(f"@{user_name}, я тебя услышал! 🌟\nРасскажи подробнее, чем помочь?")

@router.message(Command("health"))
async def cmd_health(message: types.Message):
    await message.reply("✅ Бот работает нормально!")

# ====================== ЗАПУСК ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Оптимист v2 запускается...")
    # Запускаем Telegram-бота в фоне
    asyncio.create_task(dp.start_polling(bot, drop_pending_updates=True))
    yield
    logger.info("👋 Бот остановлен")

app = FastAPI(lifespan=lifespan)

# Подключаем роутеры aiogram
dp.include_router(router)

async def main():
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())
