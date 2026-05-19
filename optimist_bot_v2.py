#!/usr/bin/env python3
"""
Оптимист v2 — идеальный Telegram-бот помощник
Полностью переписан под aiogram 3.7+
Бесплатные API: Groq (LLM) + Pollinations.ai (изображения)
Все фичи из описания + куча улучшений
"""

import os
import json
import asyncio
import logging
import datetime
import urllib.parse
from collections import defaultdict
from random import choice, random
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from dotenv import load_dotenv
import aiohttp
from zoneinfo import ZoneInfo

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("OPTIMIST_v2")

# ==================== КОНФИГ ====================
load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not TG_TOKEN:
    logger.error("❌ TG_TOKEN не найден в .env!")
    exit(1)
if not GROQ_API_KEY:
    logger.error("❌ GROQ_API_KEY не найден! Получи бесплатно: https://console.groq.com/keys")
    exit(1)

bot = Bot(token=TG_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

BOT_USERNAME: Optional[str] = None

# ==================== ХРАНИЛИЩЕ ====================
SETTINGS_FILE = "bot_settings_v2.json"

chat_settings = defaultdict(lambda: {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.25,      # вероятность ответа в группе (0.15-0.7)
    "allow_profanity": False,
    "silence_until": 0.0,
    "morning_enabled": True,
    "last_morning_sent": "",
    "horoscope_cache": {"date": "", "text": ""}
})

chat_stats = defaultdict(lambda: {
    "total_messages": 0,
    "participants": set(),
    "messages": [],              # последние 25 сообщений
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

# ==================== НАСТРОЕНИЯ ====================
MOODS = {
    "optimist": {
        "name": "😊 Оптимист",
        "emoji": "🌟",
        "prompt": """Ты — бот «Оптимист»! 
Твой стиль: невероятно позитивный, энергичный, мотивирующий и добрый. 
Всегда находи светлую сторону любой ситуации, шути, поддерживай, вдохновляй! 
Используй много радостных эмодзи (🌟😄🔥💪❤️). 
Говори так, будто общаешься с лучшим другом."""
    },
    "pessimist": {
        "name": "😔 Пессимист",
        "emoji": "💀",
        "prompt": """Ты — бот «Пессимист» с чёрным юмором.
Стиль: саркастичный, честный, немного мрачный, но не токсичный. 
Предупреждай о рисках и подводных камнях, шути над неудачами, но всегда оставляй крошечную надежду в конце. 
Эмодзи: 😕💀🤷‍♂️😔"""
    },
    "humor": {
        "name": "🤣 Юморист",
        "emoji": "😂",
        "prompt": """Ты — профессиональный стендап-комик!
Стиль: всё превращай в ржач, используй каламбуры, мемы, самоиронию, абсурд. 
Главная задача — рассмешить человека. 
Эмодзи: 😂🤣😆🔥"""
    },
    "investor_genius": {
        "name": "💰 Гений инвестиций",
        "emoji": "📈",
        "prompt": """Ты — гений трейдинга и инвестиций (с юмором).
Стиль: говори про хайпы, токены, риски, диверсификацию, FOMO, но с сарказмом и мемами. 
Фразы вроде «Это не пирамида, это возможность 100x!» или «DYOR, но я бы зашёл». 
Эмодзи: 💰📈🚀💎"""
    },
    "mafioso": {
        "name": "🔪 Мафиози",
        "emoji": "🕴️",
        "prompt": """Ты — легендарный Мафиози, профи игрок в «Мафию» с многолетним опытом.
Стиль: говори круто, эпично, по понятиям. Используй мафиозный жаргон: «братва», «развод», «чисто», «по фэншую», «дон в деле», «разводим мирняк».
Ты знаешь все роли наизусть (Дон, Мафия, Доктор, Комиссар, Самоубийца, Бомж, Любовница, Хакер, Телохранитель, Камикадзе, Шпион, Сержант, Счастливчик, Маньяк, Медсестра, Журналист, Адвокат, Оборотень и др.).
Давай профессиональные советы, объясняй стратегии, помогай выигрывать. Отвечай как настоящий босс мафии — уверенно, с уважением к братве и презрением к лохам.
Эмодзи: 🔪🕴️💼🔥"""
    }
}

LENGTH_INSTRUCTIONS = {
    "short": "Ответь ОЧЕНЬ коротко — максимум 1-2 предложения + 1-2 эмодзи. Живо и по делу.",
    "medium": "Ответь средне — один красивый абзац (4-7 предложений) с эмодзи и эмоцией.",
    "long": "Дай развёрнутый ответ: 2-3 абзаца, с советами, примерами и вдохновлением."
}

# ==================== УТРЕННИЕ ЦИТАТЫ ====================
OPTIMISTIC_QUOTES = [
    "«Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное!» 🌅",
    "«Солнце всегда встаёт после самой тёмной ночи. Ты справишься!» ☀️",
    "«Маленькие шаги каждый день приводят к большим победам.» 🚀",
    "«Ты сильнее, чем думаешь. Сегодня — твой день!» 💪",
    "«Улыбка — это самый короткий путь к счастью. Улыбнись миру!» 😊",
    "«Проблемы — это просто возможности в красивой упаковке.» 🎁",
    "«Верь в себя, и вселенная поверит в тебя.» ✨",
    "«Сегодня — лучший день, чтобы начать что-то новое!» 🌟",
    "«Ты не один. Мы вместе идём к успеху!» 🤝",
    "«Каждый закат — это обещание нового рассвета.» 🌇",
    "«Делай то, что любишь, и деньги и успех придут сами.» ❤️",
    "«Твоя энергия притягивает всё хорошее. Излучай позитив!» 🔥",
    "«Мечтай смело — Вселенная любит смельчаков!» 🌌",
    "«Сегодня ты на шаг ближе к своей лучшей версии.» 🦋",
    "«Счастье — это не цель, а способ путешествовать.» 😌"
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
    mood_key = s["mood"]
    length = s["response_length"]
    allow_prof = s["allow_profanity"]

    mood = MOODS.get(mood_key, MOODS["optimist"])
    context_list = chat_stats[cid]["messages"][-12:]
    context = "\n".join([m["text"] for m in context_list]) if context_list else "Чат только начался, контекста пока мало."

    prof_instruction = (
        "Можешь использовать нецензурную лексику и мат (в меру и по делу) для разрядки атмосферы."
        if allow_prof else
        "СТРОГО без мата, грубых слов и нецензурщины."
    )

    system_prompt = f"""{mood['prompt']}

{ LENGTH_INSTRUCTIONS.get(length, LENGTH_INSTRUCTIONS["medium"]) }

{prof_instruction}

Ты в Telegram-чате. 
ОБЯЗАТЕЛЬНО начинай ответ с упоминания пользователя в формате: @{user_name}, 
Затем продолжай естественно, тепло и по делу. 
Не повторяй контекст дословно — используй его для понимания темы.

Недавние сообщения в чате:
{context}

Отвечай ТОЛЬКО текстом сообщения. Никаких «Конечно!», «Вот мой ответ» и т.д."""

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "llama-3.1-70b-versatile",   # Лучшее качество бесплатно (на 2026)
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.82 if mood_key in ["optimist", "humor"] else 0.68,
        "max_tokens": 900 if length == "long" else 450 if length == "medium" else 220,
        "top_p": 0.9
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=28)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"].strip()
                    return content
                else:
                    err = await resp.text()
                    logger.error(f"Groq HTTP {resp.status}: {err}")
    except Exception as e:
        logger.error(f"Groq API error: {e}")

    # === Fallback (умный локальный) ===
    return local_fallback(user_text, user_name, mood_key)

def local_fallback(text: str, name: str, mood: str) -> str:
    if mood == "optimist":
        return f"Привет, {name}! {text} — это просто новый уровень! 🌟 Всё будет ОГОНЬ, я в тебя верю! 💪"
    elif mood == "pessimist":
        return f"Эххх, {name}... {text} звучит как начало грустной истории. Но кто знает, может в конце будет твист 😕"
    elif mood == "humor":
        return f"ХАААААА, {name}! {text} 😂 Это же чистый стендап-материал! Продолжай, я ржу!"
    else:
        return f"{name}, по поводу {text}... В инвестициях главное — не покупай на пике и не продавай в панике. DYOR, бро! 💰📉"

# ==================== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ (Pollinations.ai — БЕСПЛАТНО) ====================
async def generate_image_url(prompt: str, style: str = "реализм") -> Optional[str]:
    try:
        full = f"{prompt}, {style}, high detail, 8k, masterpiece"
        encoded = urllib.parse.quote(full)
        # Flux — лучшее качество на 2026
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=832&height=832&model=flux&safe=false&nologo=true&seed=-1"
        return url
    except Exception as e:
        logger.error(f"Pollinations error: {e}")
        return None

# ==================== МЕНЮ ====================
def create_settings_menu(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cid = str(chat_id)
    s = chat_settings[cid]
    mood = MOODS[s["mood"]]
    length_name = {"short": "Коротко", "medium": "Средне", "long": "Развёрнуто"}[s["response_length"]]
    act_pct = int(s["activity_level"] * 100)
    prof = "✅ Вкл" if s["allow_profanity"] else "❌ Выкл"
    morning = "🌅 Вкл" if s.get("morning_enabled", True) else "🌅 Выкл"

    text = (
        f"⚙️ <b>Настройки «Оптимиста»</b>\n\n"
        f"{mood['emoji']} <b>Настроение:</b> {mood['name']}\n"
        f"📝 <b>Длина ответов:</b> {length_name}\n"
        f"📊 <b>Активность в группе:</b> {act_pct}%\n"
        f"🤬 <b>Нецензурщина:</b> {prof}\n"
        f"🌅 <b>Утреннее приветствие (8:00 МСК):</b> {morning}\n\n"
        f"<i>Выбери, что хочешь изменить:</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist"),
            InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")
        ],
        [
            InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor"),
            InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")
        ],
        [
            InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")
        ],
        [
            InlineKeyboardButton(text="📝 Коротко", callback_data="len_short"),
            InlineKeyboardButton(text="📝 Средне", callback_data="len_medium"),
            InlineKeyboardButton(text="📝 Развёрнуто", callback_data="len_long")
        ],
        [
            InlineKeyboardButton(text="📉 15%", callback_data="act_low"),
            InlineKeyboardButton(text="📊 35%", callback_data="act_med"),
            InlineKeyboardButton(text="📈 70%", callback_data="act_high")
        ],
        [
            InlineKeyboardButton(text="🤬 Мат Вкл/Выкл", callback_data="toggle_profanity")
        ],
        [
            InlineKeyboardButton(text="🌅 Утро Вкл/Выкл", callback_data="toggle_morning")
        ],
        [
            InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")
        ]
    ])
    return text, kb

# ==================== ХЕНДЛЕРЫ ====================
@router.message(Command("start", "menu", "admin", "help", "stats"))
async def cmd_start_menu(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    if chat_id < 0:  # группа
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            if member.status not in ["administrator", "creator"]:
                await message.reply("🔒 Настройки бота доступны только администраторам группы!")
                return
        except Exception:
            pass

    if message.text == "/stats":
        cid = str(chat_id)
        stats = chat_stats[cid]
        total = stats["total_messages"]
        users = len(stats["participants"])
        today = datetime.date.today().isoformat()
        daily = stats["daily_messages"].get(today, 0)
        text = (
            f"📊 <b>Статистика чата</b>\n\n"
            f"Всего сообщений: <b>{total}</b>\n"
            f"Участников: <b>{users}</b>\n"
            f"Сегодня: <b>{daily}</b>\n"
            f"Активность бота: <b>{int(chat_settings[cid]['activity_level']*100)}%</b>"
        )
        await message.reply(text)
        return

    text, kb = create_settings_menu(chat_id)
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data.startswith("mood_"))
async def cb_change_mood(call: CallbackQuery):
    mood_key = call.data.replace("mood_", "")
    cid = str(call.message.chat.id)
    chat_settings[cid]["mood"] = mood_key
    save_settings()

    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer(f"✅ {MOODS[mood_key]['name']}")

@router.callback_query(F.data.startswith("len_"))
async def cb_change_length(call: CallbackQuery):
    length = call.data.replace("len_", "")
    cid = str(call.message.chat.id)
    chat_settings[cid]["response_length"] = length
    save_settings()

    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer("✅ Длина ответов изменена!")

@router.callback_query(F.data.startswith("act_"))
async def cb_change_activity(call: CallbackQuery):
    level_map = {"low": 0.15, "med": 0.35, "high": 0.70}
    level = level_map.get(call.data.replace("act_", ""), 0.25)
    cid = str(call.message.chat.id)
    chat_settings[cid]["activity_level"] = level
    save_settings()

    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer(f"✅ Активность: {int(level*100)}%")

@router.callback_query(F.data == "toggle_profanity")
async def cb_toggle_profanity(call: CallbackQuery):
    cid = str(call.message.chat.id)
    chat_settings[cid]["allow_profanity"] = not chat_settings[cid]["allow_profanity"]
    save_settings()

    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    await call.answer("✅ Настройка нецензурщины изменена!")

@router.callback_query(F.data == "toggle_morning")
async def cb_toggle_morning(call: CallbackQuery):
    cid = str(call.message.chat.id)
    chat_settings[cid]["morning_enabled"] = not chat_settings[cid].get("morning_enabled", True)
    save_settings()

    text, kb = create_settings_menu(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    status = "Вкл 🌅" if chat_settings[cid]["morning_enabled"] else "Выкл"
    await call.answer(f"✅ Утреннее приветствие: {status}")

@router.callback_query(F.data == "close_menu")
async def cb_close_menu(call: CallbackQuery):
    await call.message.delete()
    await call.answer("Меню закрыто 👋")

# ==================== РИСОВАНИЕ ====================
@router.message(F.text.lower().regexp(r"^(нарисуй|нарисуй мне|покажи картинку|сгенерируй картинку)"))
async def cmd_draw(message: types.Message):
    text = message.text.lower()
    prompt = text

    for prefix in ["нарисуй мне", "нарисуй", "покажи картинку", "сгенерируй картинку"]:
        if text.startswith(prefix):
            prompt = text[len(prefix):].strip()
            break

    style = "реализм, детализировано, cinematic"
    if "в стиле" in prompt:
        parts = prompt.split("в стиле")
        prompt = parts[0].strip()
        style = parts[1].strip() + ", high quality"

    if not prompt or len(prompt) < 3:
        await message.reply("🖼️ Напиши, что нарисовать, например:\n<code>нарисуй кота в стиле аниме</code>")
        return

    await message.reply(f"🎨 Рисую <b>{prompt}</b>...\nСтиль: {style}")

    url = await generate_image_url(prompt, style)
    if url:
        try:
            await bot.send_photo(
                message.chat.id,
                photo=url,
                caption=f"✨ {prompt}\nСтиль: {style}"
            )
        except Exception as e:
            logger.error(f"send_photo error: {e}")
            await message.reply("😔 Не получилось отправить картинку. Попробуй другой запрос!")
    else:
        await message.reply("😔 Не удалось сгенерировать изображение. Попробуй чуть позже или другой промпт!")

@router.message(F.text.lower().startswith("сгенерируй стикер"))
async def cmd_sticker(message: types.Message):
    prompt = message.text.lower().replace("сгенерируй стикер", "").strip()
    if not prompt:
        await message.reply("🎉 О чём сделать стикер? Например: <code>сгенерируй стикер грустный кот</code>")
        return

    await message.reply(f"🎉 Создаю стикер: {prompt}...")

    url = await generate_image_url(prompt, "милый стикер, белый фон, минимализм, emoji style")
    if url:
        await bot.send_photo(
            message.chat.id,
            photo=url,
            caption="Готово! Сохрани картинку → добавь как стикер в Telegram"
        )
    else:
        await message.reply("😞 Не получилось сделать стикер...")


# ==================== ГОРОСКОП (генерируется LLM) ====================
async def generate_horoscope(chat_id: int, user_name: str) -> str:
    """Генерирует оптимистичный гороскоп на сегодня через Groq"""
    cid = str(chat_id)
    today = datetime.date.today().isoformat()
    cache = chat_settings[cid].get("horoscope_cache", {"date": "", "text": ""})

    # Если уже есть на сегодня — возвращаем из кэша
    if cache.get("date") == today and cache.get("text"):
        return cache["text"]

    mood_key = chat_settings[cid]["mood"]
    mood_name = MOODS.get(mood_key, MOODS["optimist"])["name"]

    prompt = f"""Ты — профессиональный астролог-оптимист с отличным чувством юмора.
Напиши короткий (5-7 предложений), очень позитивный, вдохновляющий и мотивирующий гороскоп на сегодня ({today}) для пользователя по имени {user_name}.
Сделай его личным, тёплым и полным энергии.
Используй 3-4 эмодзи.
Тон: {mood_name}.
Гороскоп должен поднимать настроение и давать лёгкий совет на день.
Не упоминай конкретные знаки зодиака — сделай универсальный, но очень приятный текст."""

    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [
                {"role": "system", "content": "Ты — добрый и мудрый астролог-оптимист."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.85,
            "max_tokens": 280
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers, json=payload, timeout=15
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    horo = data["choices"][0]["message"]["content"].strip()

                    # Сохраняем в кэш
                    chat_settings[cid]["horoscope_cache"] = {"date": today, "text": horo}
                    save_settings()
                    return horo
    except Exception as e:
        logger.error(f"Horoscope generation error: {e}")

    # Fallback
    return f"Сегодня звёзды на твоей стороне, {user_name}! 🌟 Всё получится лучше, чем ты ожидаешь. Просто действуй с улыбкой! 💫"


@router.message(Command("horoscope", "гороскоп", "гороскоп на сегодня"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    await message.reply("🔮 Генерирую твой персональный оптимистичный гороскоп на сегодня...")

    horo = await generate_horoscope(message.chat.id, user_name)
    await message.reply(horo)


# ==================== АНАЛИТИКА ЧАТА (для Мафиози) ====================
async def analyze_chat(chat_id: int) -> str:
    """Генерирует мафиозную аналитику чата"""
    cid = str(chat_id)
    stats = chat_stats[cid]
    recent = stats["messages"][-15:] if stats["messages"] else []

    if not recent:
        return "Чат ещё слишком тихий, братва. Нужно больше разговоров для нормального расклада."

    # Простая статистика
    activity = {}
    for m in recent:
        # Простой подсчёт (в реальности можно улучшить)
        pass

    prompt = f"""Ты — легендарный Мафиози-аналитик.
Проанализируй последние 15 сообщений чата и дай крутой, эпичный расклад по понятиям.

Последние сообщения:
{chr(10).join([m['text'][:120] for m in recent])}

Дай анализ в стиле мафиози:
- Кто слишком активный (возможно мирняк или развод)
- Кто слишком тихий (подозрительно)
- Возможные противоречия
- Кого стоит проверить/убить сегодня
- Общий вердикт: "Чисто" или "Там пахнет мафией"

Отвечай коротко, но по делу, как настоящий босс."""

    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.1-70b-versatile",
            "messages": [
                {"role": "system", "content": "Ты — топовый мафиози-аналитик."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "max_tokens": 350
        }
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=18) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Анализ чата ошибка: {e}")

    return "Братва, что-то с анализом не сложилось. Давай на глаз: этот тип слишком много молчит — проверяй его первым."


@router.message(Command("analyze", "анализ", "расклад", "анализируй"))
async def cmd_analyze(message: types.Message):
    mood = chat_settings[str(message.chat.id)]["mood"]
    if mood != "mafioso":
        await message.reply("Этот режим аналитики работает только в режиме **🔪 Мафиози**. Переключись через /menu")
        return

    await message.reply("🕵️‍♂️ Провожу глубокий мафиозный анализ чата...")
    analysis = await analyze_chat(message.chat.id)
    await message.reply(analysis)


# ==================== ОСНОВНОЙ ХЕНДЛЕР ====================
@router.message()
async def message_handler(message: types.Message):
    if not message.text:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "друг"
    text = message.text.strip()

    update_chat_stats(chat_id, user_id, text)
    cid = str(chat_id)
    s = chat_settings[cid]

    # Тишина
    if s["silence_until"] > datetime.datetime.now().timestamp():
        return

    if any(word in text.lower() for word in ["помолчи", "тихо", "молчи 15"]):
        s["silence_until"] = datetime.datetime.now().timestamp() + 900
        save_settings()
        await message.reply("🤫 Хорошо, молчу 15 минут. Позови, если что!")
        return

    # === ЛОГИКА ДЛЯ ГРУПП ===
    is_group = chat_id < 0
    mentioned = False

    if is_group:
        lower_text = text.lower()
        if BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lower_text:
            mentioned = True
        elif "оптимист" in lower_text or "бот" in lower_text:
            mentioned = True

        if not mentioned and random() > s["activity_level"]:
            # лёгкая реакция
            if random() < 0.35:
                sentiment = 0.7 if any(w in lower_text for w in ["хорошо", "круто", "супер"]) else 0.4
                reactions = ["👍", "🔥", "😊", "❤️"] if sentiment > 0.5 else ["🤔", "😐", "💭"]
                try:
                    await message.reply(choice(reactions))
                except:
                    pass
            return

    # === ГЕНЕРАЦИЯ ОТВЕТА ===
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    response = await get_llm_response(text, chat_id, user_name)

    try:
        await message.reply(response)
    except Exception as e:
        logger.error(f"reply error: {e}")
        await message.reply("Что-то пошло не так... но я всё равно тебя люблю! ❤️")

    # случайная реакция в группе
    if is_group and random() < 0.12:
        try:
            await message.reply(choice(["🌟", "💪", "😎", "🔥", "❤️"]))
        except:
            pass


# ==================== УТРЕННЕЕ ПРИВЕТСТВИЕ (8:00 МСК) ====================
async def get_rates() -> dict:
    """Бесплатные курсы: BTC, USDT, USD, EUR к RUB"""
    try:
        async with aiohttp.ClientSession() as session:
            # CoinGecko (BTC + USDT)
            cg_url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=rub"
            async with session.get(cg_url, timeout=8) as resp:
                cg = await resp.json()
                btc = round(cg["bitcoin"]["rub"])
                usdt = round(cg["tether"]["rub"])

            # ЦБ РФ (USD + EUR)
            cbr_url = "https://www.cbr-xml-daily.ru/daily_json.js"
            async with session.get(cbr_url, timeout=8) as resp:
                cbr = await resp.json()
                usd = round(cbr["Valute"]["USD"]["Value"], 2)
                eur = round(cbr["Valute"]["EUR"]["Value"], 2)

            return {"btc": btc, "usdt": usdt, "usd": usd, "eur": eur}
    except Exception as e:
        logger.warning(f"Не удалось получить курсы: {e}")
        return {"btc": "—", "usdt": "—", "usd": "—", "eur": "—"}


async def send_morning_greeting(chat_id: int):
    """Отправляет красивое утреннее сообщение"""
    quote = choice(OPTIMISTIC_QUOTES)
    rates = await get_rates()

    text = (
        f"🌅 <b>Доброе утро, друг!</b>\n\n"
        f"{quote}\n\n"
        f"💰 <b>Курсы на сегодня (МСК):</b>\n"
        f"• Bitcoin (BTC): <b>{rates['btc']} ₽</b>\n"
        f"• USDT: <b>{rates['usdt']} ₽</b>\n"
        f"• Доллар США (USD): <b>{rates['usd']} ₽</b>\n"
        f"• Евро (EUR): <b>{rates['eur']} ₽</b>\n\n"
        f"Пусть сегодняшний день принесёт тебе много радости, энергии и маленьких побед! 💪✨\n"
        f"Чем планируешь заняться? Я всегда рядом! 🌟"
    )
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
        logger.info(f"🌅 Утреннее приветствие отправлено в чат {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки утреннего сообщения в {chat_id}: {e}")


async def morning_greeting_loop():
    """Фоновая задача: проверяет 8:00 МСК каждый 60 секунд"""
    msk = ZoneInfo("Europe/Moscow")
    while True:
        try:
            now = datetime.datetime.now(msk)
            if now.hour == 8 and now.minute < 5:  # окно 8:00–8:05
                today = now.date().isoformat()
                for cid_str, settings in list(chat_settings.items()):
                    if settings.get("morning_enabled", True) and settings.get("last_morning_sent", "") != today:
                        try:
                            await send_morning_greeting(int(cid_str))
                            chat_settings[cid_str]["last_morning_sent"] = today
                            save_settings()
                        except Exception as e:
                            logger.error(f"Ошибка утреннего приветствия для {cid_str}: {e}")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Ошибка в morning loop: {e}")
            await asyncio.sleep(60)


# ==================== ЗАПУСК ====================
async def on_startup():
    global BOT_USERNAME
    load_settings()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logger.info(f"🚀 Бот @{BOT_USERNAME} (Оптимист v2) успешно запущен!")
    logger.info("🌟 Groq + Pollinations.ai подключены. Бот идеален!")

    # Запускаем утреннее приветствие
    asyncio.create_task(morning_greeting_loop())
    logger.info("🌅 Фоновая задача утренних приветствий запущена (8:00 МСК)")

async def main():
    dp.include_router(router)
    await on_startup()
    await dp.start_polling(
        bot,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен вручную")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")