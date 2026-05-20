#!/usr/bin/env python3
"""
Оптимист complete final — Telegram AI bot.

Функции:
- Groq + Gemini fallback для ответов.
- FusionBrain для генерации изображений + Pollinations fallback.
- Настройки через inline меню, менять могут только админы групп.
- Режим «только по запросу» и режим сна «Оптимист спи/спать».
- Точная интенсивность активности: 0%, 10%, 20% ... 100%.
- Настоящие Telegram reactions через setMessageReaction, а не ответы-смайлики.
- Тематические стикеры из чата: бот запоминает стикеры и может отправлять их по контексту.
- /summary — нейтральное резюме обсуждения за 1/3/6/24 часа или произвольное число часов.
- /analyze — отдельная мафиозная аналитика как ведущий игры в Мафию.
- Утреннее приветствие с курсами валют с несколькими fallback-источниками.
"""

import os
import json
import asyncio
import logging
import urllib.parse
import datetime
import base64
import re
import threading
import http.server
import socketserver
from collections import defaultdict
from random import choice, random
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
)
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("OPTIMIST_COMPLETE_FINAL")

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FUSION_BRAIN_API_KEY = os.getenv("FUSION_BRAIN_API_KEY")
FUSION_BRAIN_SECRET_KEY = os.getenv("FUSION_BRAIN_SECRET_KEY")

GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MORNING_TZ = os.getenv("MORNING_TZ", "Europe/Moscow")
MORNING_HOUR = int(os.getenv("MORNING_HOUR", "8"))
MENU_DELETE_SECONDS = int(os.getenv("MENU_DELETE_SECONDS", "2"))
MENU_AUTO_TTL_SECONDS = int(os.getenv("MENU_AUTO_TTL_SECONDS", "120"))
SUMMARY_MAX_MESSAGES = int(os.getenv("SUMMARY_MAX_MESSAGES", "3000"))
CONTEXT_MAX_MESSAGES = int(os.getenv("CONTEXT_MAX_MESSAGES", "25"))
STICKERS_MAX_PER_CHAT = int(os.getenv("STICKERS_MAX_PER_CHAT", "80"))

if not TG_TOKEN:
    logger.error("TG_TOKEN не найден!")
    raise SystemExit(1)

bot = Bot(token=TG_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
BOT_USERNAME: Optional[str] = None
BOT_ID: Optional[int] = None

# ==================== HTTP HEALTHCHECK ====================
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
        return


def start_http_server():
    port = int(os.environ.get("PORT", 8000))
    try:
        with socketserver.TCPServer(("", port), HealthHandler) as httpd:
            logger.info(f"🌐 HTTP health server on port {port}")
            httpd.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server error: {e}")

# ==================== ХРАНИЛИЩЕ ====================
SETTINGS_FILE = "bot_settings_complete_final.json"

DEFAULT_CHAT_SETTINGS = {
    "mood": "optimist",
    "response_length": "medium",
    "activity_level": 0.10,
    "request_only": False,
    "sleep_mode": False,
    "allow_profanity": False,
    "silence_until": 0.0,
    "morning_enabled": True,
    "last_morning_sent": "",
    "horoscope_cache": {},
    "context_reactions": True,
    "stickers_enabled": True,
    "summary_schedule_enabled": False,
    "summary_interval_hours": 6,
    "last_summary_sent_ts": 0.0,
}

chat_settings: defaultdict[str, Dict[str, Any]] = defaultdict(lambda: dict(DEFAULT_CHAT_SETTINGS))
chat_stats: defaultdict[str, Dict[str, Any]] = defaultdict(lambda: {
    "total_messages": 0,
    "participants": set(),
    "messages": [],
    "daily_messages": defaultdict(int),
    "weekly_messages": defaultdict(int),
})
chat_stickers: defaultdict[str, List[Dict[str, str]]] = defaultdict(list)


def load_settings():
    global chat_settings, chat_stats, chat_stickers
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for cid, s in data.get("chat_settings", {}).items():
            merged = dict(DEFAULT_CHAT_SETTINGS)
            merged.update(s)
            chat_settings[cid].update(merged)

        for cid, s in data.get("chat_stats", {}).items():
            chat_stats[cid].update({
                "total_messages": s.get("total_messages", 0),
                "participants": set(s.get("participants", [])),
                "messages": s.get("messages", [])[-SUMMARY_MAX_MESSAGES:],
                "daily_messages": defaultdict(int, s.get("daily_messages", {})),
                "weekly_messages": defaultdict(int, s.get("weekly_messages", {})),
            })

        for cid, stickers in data.get("chat_stickers", {}).items():
            if isinstance(stickers, list):
                chat_stickers[cid] = stickers[-STICKERS_MAX_PER_CHAT:]

        logger.info("✅ Настройки загружены")
    except FileNotFoundError:
        logger.info("📁 Новый файл настроек создан")
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {e}")


def save_settings():
    data = {
        "chat_settings": {k: dict(v) for k, v in chat_settings.items()},
        "chat_stats": {
            k: {
                "total_messages": v.get("total_messages", 0),
                "participants": list(v.get("participants", set())),
                "messages": v.get("messages", [])[-SUMMARY_MAX_MESSAGES:],
                "daily_messages": dict(v.get("daily_messages", {})),
                "weekly_messages": dict(v.get("weekly_messages", {})),
            }
            for k, v in chat_stats.items()
        },
        "chat_stickers": {k: v[-STICKERS_MAX_PER_CHAT:] for k, v in chat_stickers.items()},
    }
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {e}")


def update_chat_stats(chat_id: int, user_id: int, text: str, user_name: str = ""):
    cid = str(chat_id)
    stats = chat_stats[cid]
    today = datetime.date.today().isoformat()
    week = str(datetime.datetime.now().isocalendar()[1])
    stats["total_messages"] += 1
    stats["participants"].add(user_id)
    stats["daily_messages"][today] += 1
    stats["weekly_messages"][week] += 1
    stats["messages"].append({
        "text": text[:1200],
        "user": user_name[:80],
        "user_id": user_id,
        "ts": datetime.datetime.now().timestamp(),
    })
    if len(stats["messages"]) > SUMMARY_MAX_MESSAGES:
        stats["messages"] = stats["messages"][-SUMMARY_MAX_MESSAGES:]

# ==================== НАСТРОЕНИЯ ====================
MOODS = {
    "optimist": {
        "name": "😊 Оптимист",
        "emoji": "🌟",
        "prompt": "Ты — жизнерадостный оптимист. Поддерживаешь, вдохновляешь, отвечаешь живо. Не повторяй вопрос пользователя.",
    },
    "pessimist": {
        "name": "😔 Пессимист",
        "emoji": "💀",
        "prompt": "Ты — саркастичный пессимист с чёрным юмором. Предупреждаешь о рисках, но не токсично. Не повторяй вопрос пользователя.",
    },
    "humor": {
        "name": "🤣 Юморист",
        "emoji": "😂",
        "prompt": "Ты — стендап-комик. Отвечаешь шутками и мемами, но по делу. Не повторяй запрос.",
    },
    "investor_genius": {
        "name": "💰 Гений инвестиций",
        "emoji": "📈",
        "prompt": "Ты — эксперт по хайп-проектам, криптовалютам и инвестициям. Говори про риски, FOMO, токеномику, памп/дамп. Добавляй иронию, но не давай финансовых гарантий.",
    },
    "mafioso": {
        "name": "🔪 Мафиози",
        "emoji": "🕴️",
        "prompt": """Ты — старый дон в стиле Дон Корлеоне из классической игры в Мафию.
Говори спокойно, веско, с достоинством и лёгкой скрытой угрозой.
Используй отсылки к игре: мирный житель, шериф, дон, любовница, алиби, ночь, проверка, подозрительный, голосование.
Не говори как уличная братва. Ты авторитетный дон, а не дворовый пацан.
Отвечай коротко, с юмором и иронией. Не повторяй вопрос пользователя.""",
    },
}

RESPONSE_LENGTHS = {
    "short": {"name": "Короткий", "max_tokens": 220, "rule": "Ответь очень коротко: 1-2 предложения."},
    "medium": {"name": "Средний", "max_tokens": 550, "rule": "Ответь одним компактным абзацем: 4-7 предложений."},
    "long": {"name": "Развёрнутый", "max_tokens": 950, "rule": "Дай развёрнутый ответ: 2-3 абзаца."},
}

ACTIVITY_LEVELS = {
    "0": {"name": "0%", "value": 0.00},
    "10": {"name": "10%", "value": 0.10},
    "20": {"name": "20%", "value": 0.20},
    "30": {"name": "30%", "value": 0.30},
    "40": {"name": "40%", "value": 0.40},
    "50": {"name": "50%", "value": 0.50},
    "60": {"name": "60%", "value": 0.60},
    "70": {"name": "70%", "value": 0.70},
    "80": {"name": "80%", "value": 0.80},
    "90": {"name": "90%", "value": 0.90},
    "100": {"name": "100%", "value": 1.00},
}

SUMMARY_INTERVALS = [1, 3, 6, 24]

OPTIMISTIC_QUOTES = [
    "«Каждый новый день — это чистый лист. Напиши на нём что-то прекрасное!» 🌅",
    "«Солнце всегда встаёт после самой тёмной ночи. Ты справишься!» ☀️",
    "«Маленькие шаги каждый день приводят к большим победам.» 🚀",
    "«Ты сильнее, чем думаешь. Сегодня — твой день!» 💪",
]

# ==================== УТИЛИТЫ ====================
def html_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_activity_name(value: float) -> str:
    pct = int(round(float(value) * 100))
    key = str(pct)
    if key in ACTIVITY_LEVELS:
        return ACTIVITY_LEVELS[key]["name"]
    nearest = min(ACTIVITY_LEVELS, key=lambda k: abs(int(k) - pct))
    return ACTIVITY_LEVELS[nearest]["name"]


def parse_hours_arg(text: str, default: int = 1) -> int:
    parts = text.strip().split()
    if len(parts) < 2:
        return default
    raw = parts[1].lower().strip()
    mapping = {"час": 1, "1ч": 1, "3ч": 3, "6ч": 6, "день": 24, "сутки": 24, "24ч": 24, "day": 24}
    if raw in mapping:
        return mapping[raw]
    m = re.search(r"\d+", raw)
    if not m:
        return default
    return max(1, min(int(m.group(0)), 168))


def get_recent_messages(chat_id: int, hours: Optional[int] = None, limit: int = 25) -> List[Dict[str, Any]]:
    cid = str(chat_id)
    messages = chat_stats[cid].get("messages", [])
    if hours is not None:
        start_ts = datetime.datetime.now().timestamp() - hours * 3600
        messages = [m for m in messages if float(m.get("ts", 0)) >= start_ts]
    return messages[-limit:]


def format_messages_for_llm(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        user = m.get("user") or "участник"
        text = m.get("text") or ""
        if text.strip():
            lines.append(f"{user}: {text}")
    return "\n".join(lines)


def is_direct_request(message: types.Message) -> bool:
    if message.chat.id > 0:
        return True
    lower = (message.text or "").lower()
    mention = bool(BOT_USERNAME and f"@{BOT_USERNAME.lower()}" in lower)
    by_name = "оптимист" in lower
    reply_to_bot = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and BOT_ID
        and message.reply_to_message.from_user.id == BOT_ID
    )
    return mention or by_name or reply_to_bot


def clean_user_text_for_llm(text: str) -> str:
    if BOT_USERNAME:
        text = re.sub(fr"@{re.escape(BOT_USERNAME)}", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bоптимист\b[:,]?", "", text, flags=re.IGNORECASE).strip()
    return text or "Ответь на сообщение пользователя."


async def is_admin(chat_id: int, user_id: int) -> bool:
    if chat_id > 0:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception as e:
        logger.warning(f"Не удалось проверить админа: {e}")
        return False


async def require_admin_callback(call: CallbackQuery) -> bool:
    if not call.message:
        await call.answer("Не удалось определить чат", show_alert=True)
        return False
    ok = await is_admin(call.message.chat.id, call.from_user.id)
    if not ok:
        await call.answer("⚠️ Настройки может менять только администратор чата", show_alert=True)
        return False
    return True


async def delete_message_later(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def temporary_menu(message: types.Message, text: str, reply_markup: InlineKeyboardMarkup):
    sent = await message.reply(text, reply_markup=reply_markup)
    if MENU_AUTO_TTL_SECONDS > 0:
        asyncio.create_task(delete_message_later(sent.chat.id, sent.message_id, MENU_AUTO_TTL_SECONDS))
    return sent

# ==================== LLM ====================
async def ask_llm(system_prompt: str, user_text: str, max_tokens: int, temperature: float = 0.8) -> Optional[str]:
    """Сначала Groq, затем быстрый Groq, затем Gemini."""
    if GROQ_API_KEY:
        for model in [GROQ_MODEL, GROQ_FAST_MODEL]:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_text},
                            ],
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                        },
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data["choices"][0]["message"]["content"].strip()
                        err = await resp.text()
                        logger.warning(f"Groq {model} вернул {resp.status}: {err[:300]}")
            except Exception as e:
                logger.warning(f"Groq {model} ошибка: {e}")

    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [
                    {"role": "user", "parts": [{"text": f"{system_prompt}\n\nСообщение пользователя:\n{user_text}"}]}
                ],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates") or []
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts and "text" in parts[0]:
                                return parts[0]["text"].strip()
                    err = await resp.text()
                    logger.warning(f"Gemini вернул {resp.status}: {err[:300]}")
        except Exception as e:
            logger.warning(f"Gemini ошибка: {e}")

    return None


async def get_llm_response(user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    s = chat_settings[cid]
    mood = MOODS.get(s.get("mood", "optimist"), MOODS["optimist"])
    length = RESPONSE_LENGTHS.get(s.get("response_length", "medium"), RESPONSE_LENGTHS["medium"])
    allow_prof = s.get("allow_profanity", False)

    recent = get_recent_messages(chat_id, limit=CONTEXT_MAX_MESSAGES)
    context = format_messages_for_llm(recent[:-1]) if recent else "Контекста пока нет."
    prof_rule = "Мат можно использовать умеренно, если он уместен." if allow_prof else "Мат, грубость и оскорбления запрещены."

    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length['rule']}\n"
        f"{prof_rule}\n"
        f"Ты общаешься в Telegram. Начинай ответ строго с @{user_name}, затем продолжай.\n"
        f"НЕ повторяй фразу пользователя. Не пиши 'ты спросил', 'по поводу', 'как я понял'.\n"
        f"Отвечай сразу по существу, учитывая контекст, но не пересказывай его дословно.\n\n"
        f"Контекст последних сообщений:\n{context}"
    )

    cleaned = clean_user_text_for_llm(user_text)
    answer = await ask_llm(system_prompt, cleaned, length["max_tokens"], temperature=0.8)
    if answer:
        return answer
    return local_fallback(user_name, s.get("mood", "optimist"))


def local_fallback(name: str, mood: str) -> str:
    if mood == "mafioso":
        return f"@{name}, я услышал. Спокойно, как на ночной проверке: сначала смотрим, потом голосуем. 🕴️"
    if mood == "investor_genius":
        return f"@{name}, мысль принята. Но помни: без DYOR любой памп превращается в урок экономики. 📈"
    reactions = {
        "optimist": f"@{name}, я рядом. Разберёмся и вытащим из этого пользу. 🌟",
        "pessimist": f"@{name}, звучит тревожно, но катастрофу пока объявлять рано. 😕",
        "humor": f"@{name}, принято. Ситуация уже смешная, но мы сделаем вид, что контролируем её. 😂",
    }
    return reactions.get(mood, f"@{name}, я тебя услышал.")

# ==================== РЕАКЦИИ И СТИКЕРЫ ====================
POSITIVE_WORDS = {"спасибо", "круто", "отлично", "супер", "ура", "кайф", "топ", "класс", "победа", "люблю", "ахаха", "хаха"}
NEGATIVE_WORDS = {"плохо", "грустно", "проблема", "ужас", "беда", "больно", "страшно", "тревога", "ошибка", "провал"}
QUESTION_WORDS = {"как", "почему", "зачем", "когда", "где", "что", "кто", "можно"}

REACTIONS = {
    "positive": ["🔥", "👍", "❤", "👏", "😁"],
    "negative": ["😢", "👎", "🤯", "😱"],
    "question": ["🤔", "👀"],
    "neutral": ["👍", "👀", "🤔"],
}

STICKER_EMOJI_GROUPS = {
    "positive": {"🔥", "👍", "❤", "😍", "😁", "😂", "👏", "🎉", "🥳"},
    "negative": {"😢", "😭", "😔", "😡", "👎", "💔", "😱"},
    "question": {"🤔", "👀", "❓", "🧐"},
    "neutral": {"👍", "👀", "🙂", "🤷"},
}


def detect_context_kind(text: str) -> str:
    lower = text.lower()
    words = set(re.findall(r"[а-яa-zё]+", lower))
    if "?" in text or words & QUESTION_WORDS:
        return "question"
    if words & POSITIVE_WORDS:
        return "positive"
    if words & NEGATIVE_WORDS:
        return "negative"
    return "neutral"


async def set_context_reaction(message: types.Message, kind: Optional[str] = None) -> bool:
    kind = kind or detect_context_kind(message.text or "")
    emoji = choice(REACTIONS.get(kind, REACTIONS["neutral"]))
    try:
        await bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
        return True
    except Exception as e:
        logger.info(f"Не удалось поставить реакцию {emoji}: {e}")
        return False


def remember_sticker(chat_id: int, sticker: types.Sticker):
    cid = str(chat_id)
    file_id = sticker.file_id
    emoji = sticker.emoji or ""
    current = chat_stickers[cid]
    if any(s.get("file_id") == file_id for s in current):
        return
    current.append({"file_id": file_id, "emoji": emoji, "set_name": sticker.set_name or ""})
    if len(current) > STICKERS_MAX_PER_CHAT:
        chat_stickers[cid] = current[-STICKERS_MAX_PER_CHAT:]
    save_settings()


async def send_context_sticker(message: types.Message, kind: Optional[str] = None) -> bool:
    cid = str(message.chat.id)
    stickers = chat_stickers[cid]
    if not stickers:
        return False
    kind = kind or detect_context_kind(message.text or "")
    preferred = STICKER_EMOJI_GROUPS.get(kind, STICKER_EMOJI_GROUPS["neutral"])
    matching = [s for s in stickers if s.get("emoji") in preferred]
    selected = choice(matching or stickers)
    try:
        await bot.send_sticker(
            chat_id=message.chat.id,
            sticker=selected["file_id"],
            reply_to_message_id=message.message_id,
        )
        return True
    except Exception as e:
        logger.info(f"Не удалось отправить стикер: {e}")
        return False

# ==================== КАРТИНКИ ====================
async def translate_to_english_for_image(text: str) -> str:
    translated = await ask_llm(
        "Переведи запрос на английский для генерации изображения. Верни только перевод без пояснений.",
        text,
        max_tokens=160,
        temperature=0.2,
    )
    return translated.strip() if translated else text


async def fusionbrain_generate_image(prompt: str, style: str = "") -> Optional[bytes]:
    if not FUSION_BRAIN_API_KEY or not FUSION_BRAIN_SECRET_KEY:
        return None

    base_url = "https://api-key.fusionbrain.ai/"
    headers = {
        "X-Key": f"Key {FUSION_BRAIN_API_KEY}",
        "X-Secret": f"Secret {FUSION_BRAIN_SECRET_KEY}",
    }
    query = prompt if not style else f"{prompt}, {style}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + "key/api/v1/pipelines", headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    logger.warning(f"FusionBrain pipelines status {resp.status}")
                    return None
                pipelines = await resp.json()
                if not pipelines:
                    return None
                pipeline_id = pipelines[0].get("id")

            params = {
                "type": "GENERATE",
                "numImages": 1,
                "width": 1024,
                "height": 1024,
                "generateParams": {"query": query},
            }
            form = aiohttp.FormData()
            form.add_field("pipeline_id", str(pipeline_id))
            form.add_field("params", json.dumps(params), content_type="application/json")

            async with session.post(base_url + "key/api/v1/pipeline/run", headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    logger.warning(f"FusionBrain run status {resp.status}: {(await resp.text())[:200]}")
                    return None
                data = await resp.json()
                uuid = data.get("uuid")
                if not uuid:
                    return None

            status_paths = [
                f"key/api/v1/pipeline/status/{uuid}",
                f"key/api/v1/text2image/status/{uuid}",
            ]
            for _ in range(20):
                await asyncio.sleep(3)
                for path in status_paths:
                    async with session.get(base_url + path, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            continue
                        st = await resp.json()
                        status = st.get("status")
                        if status == "DONE":
                            files = (st.get("result") or {}).get("files") or []
                            if files:
                                return base64.b64decode(files[0])
                        if status == "FAIL":
                            logger.warning("FusionBrain generation failed")
                            return None
        return None
    except Exception as e:
        logger.warning(f"FusionBrain error: {e}")
        return None


async def pollinations_image_url(prompt: str, style: str = "realistic, high detail") -> str:
    en_prompt = await translate_to_english_for_image(prompt)
    full = f"{en_prompt}, {style}"
    encoded = urllib.parse.quote(full)
    return f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&safe=false&nologo=true"


DRAW_PREFIXES = [
    "нарисуй мне",
    "нарисуй",
    "сделай картинку",
    "создай картинку",
    "создай изображение",
    "сгенерируй изображение",
    "сгенерируй картинку",
    "сгенерируй стикер",
    "покажи картинку",
]


def parse_draw_prompt(text: str) -> Tuple[str, str, bool]:
    raw = text.strip()
    lower = raw.lower()
    sticker_mode = "стикер" in lower
    prompt = raw
    for prefix in DRAW_PREFIXES:
        if lower.startswith(prefix):
            prompt = raw[len(prefix):].strip()
            break
    style = "sticker, clean background, high quality" if sticker_mode else "realistic, high detail, high quality"
    if " в стиле " in prompt.lower():
        parts = re.split(r"\s+в стиле\s+", prompt, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            prompt, style = parts[0].strip(), f"{parts[1].strip()}, high quality"
    return prompt, style, sticker_mode

# ==================== МЕНЮ ====================
START_TEXT = (
    "🤖 <b>Оптимист — AI-бот для Telegram</b>\n\n"
    "Я отвечаю в 5 характерах, рисую картинки, делаю резюме чата, гороскопы и мафиозный анализ.\n"
    "В группах меня можно настроить: активность, реакции, стикеры, режим только по запросу.\n\n"
    "Открой меню и выбери режим 👇"
)

ABOUT_BOT_TEXT = (
    "🤖 <b>Я — Оптимист.</b>\n"
    "Умею отвечать в 5 стилях, рисовать, делать /summary, /analyze, /horoscope и утренние сводки.\n"
    "В группе упомяни меня или напиши «оптимист», чтобы я точно ответил."
)

ABOUT_TRIGGERS = ["кто ты", "ты кто", "что ты умеешь", "твои возможности", "расскажи о себе", "что ты такое"]


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😊 Оптимист", callback_data="mood_optimist")],
        [InlineKeyboardButton(text="😔 Пессимист", callback_data="mood_pessimist")],
        [InlineKeyboardButton(text="🤣 Юморист", callback_data="mood_humor")],
        [InlineKeyboardButton(text="💰 Инвестор", callback_data="mood_investor_genius")],
        [InlineKeyboardButton(text="🔪 Мафиози", callback_data="mood_mafioso")],
        [InlineKeyboardButton(text="📝 Резюме чата", callback_data="summary_menu")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="❔ Help", callback_data="help_menu"), InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")],
    ])


def settings_keyboard(chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    s = chat_settings[str(chat_id)]
    length_name = RESPONSE_LENGTHS.get(s.get("response_length"), RESPONSE_LENGTHS["medium"])["name"]
    activity_name = get_activity_name(float(s.get("activity_level", 0.1)))
    request_only = "✅ Вкл" if s.get("request_only") else "❌ Выкл"
    sleep = "😴 Спит" if s.get("sleep_mode") else "✅ Бодрствует"
    reactions = "✅ Вкл" if s.get("context_reactions") else "❌ Выкл"
    stickers = "✅ Вкл" if s.get("stickers_enabled") else "❌ Выкл"
    prof = "✅ Вкл" if s.get("allow_profanity") else "❌ Выкл"
    morning = "✅ Вкл" if s.get("morning_enabled") else "❌ Выкл"
    auto_summary = "✅ Вкл" if s.get("summary_schedule_enabled") else "❌ Выкл"
    summary_int = s.get("summary_interval_hours", 6)

    text = (
        "⚙️ <b>Настройки чата</b>\n\n"
        f"📝 Длина ответов: <b>{length_name}</b>\n"
        f"📊 Активность без обращения: <b>{activity_name}</b>\n"
        f"🎯 Только по запросу: <b>{request_only}</b>\n"
        f"💤 Состояние: <b>{sleep}</b>\n"
        f"💬 Реакции на сообщения: <b>{reactions}</b>\n"
        f"🎭 Стикеры из чата: <b>{stickers}</b>\n"
        f"🤬 Мат: <b>{prof}</b>\n"
        f"🌅 Утро: <b>{morning}</b>\n"
        f"📝 Авто-summary: <b>{auto_summary}</b> каждые {summary_int} ч"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📝 Длина: {length_name}", callback_data="toggle_length")],
        [InlineKeyboardButton(text=f"📊 Активность: {activity_name}", callback_data="activity_menu")],
        [InlineKeyboardButton(text=f"🎯 Только по запросу: {request_only}", callback_data="toggle_request_only")],
        [InlineKeyboardButton(text=f"💤 Сон: {sleep}", callback_data="toggle_sleep")],
        [InlineKeyboardButton(text=f"💬 Реакции: {reactions}", callback_data="toggle_reactions")],
        [InlineKeyboardButton(text=f"🎭 Стикеры: {stickers}", callback_data="toggle_stickers")],
        [InlineKeyboardButton(text=f"🤬 Мат: {prof}", callback_data="toggle_profanity")],
        [InlineKeyboardButton(text=f"🌅 Утро: {morning}", callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"📝 Авто-summary: {auto_summary}", callback_data="toggle_summary_schedule")],
        [InlineKeyboardButton(text=f"⏱ Интервал summary: {summary_int}ч", callback_data="summary_interval_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu"), InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")],
    ])
    return text, kb


def activity_keyboard() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key in ACTIVITY_LEVELS:
        row.append(InlineKeyboardButton(text=ACTIVITY_LEVELS[key]["name"], callback_data=f"set_activity_{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🔙 Настройки", callback_data="settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 За 1 час", callback_data="summary_1"), InlineKeyboardButton(text="🕒 За 3 часа", callback_data="summary_3")],
        [InlineKeyboardButton(text="🕕 За 6 часов", callback_data="summary_6"), InlineKeyboardButton(text="📅 За день", callback_data="summary_24")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu"), InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")],
    ])


def summary_interval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1ч", callback_data="set_summary_interval_1"), InlineKeyboardButton(text="3ч", callback_data="set_summary_interval_3")],
        [InlineKeyboardButton(text="6ч", callback_data="set_summary_interval_6"), InlineKeyboardButton(text="24ч", callback_data="set_summary_interval_24")],
        [InlineKeyboardButton(text="🔙 Настройки", callback_data="settings")],
    ])

HELP_TEXT = (
    "📋 <b>Команды Оптимиста</b>\n\n"
    "/start — краткое приветствие и меню\n"
    "/menu — меню режимов и настроек\n"
    "/help — список команд\n"
    "/stats — статистика чата\n"
    "/summary [1/3/6/24] — нейтральное резюме обсуждения\n"
    "/analyze или /анализ — мафиозный анализ как ведущий игры\n"
    "/horoscope или /гороскоп [знак] — гороскоп\n\n"
    "🎨 <b>Рисование:</b>\n"
    "нарисуй кота в киберпанке\n"
    "сгенерируй стикер грустный хомяк\n"
    "сделай картинку город будущего в стиле неон\n\n"
    "🤫 <b>Тишина:</b>\n"
    "помолчи / тихо / молчи 15 — тишина на 15 минут\n"
    "оптимист спи / оптимист спать — полный сон до обращения\n"
    "оптимист проснись — разбудить\n\n"
    "💡 В группе я гарантированно отвечаю на упоминание, ответ на моё сообщение или слово «оптимист»."
)

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await temporary_menu(message, START_TEXT, main_menu_keyboard())

@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await temporary_menu(message, "🎭 <b>Меню Оптимиста</b>", main_menu_keyboard())

@router.callback_query(F.data == "close_menu")
async def close_menu(call: CallbackQuery):
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Меню закрыто")

@router.callback_query(F.data == "help_menu")
async def help_menu(call: CallbackQuery):
    await call.message.edit_text(HELP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu"), InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")]
    ]))
    await call.answer()

@router.callback_query(F.data.startswith("mood_"))
async def change_mood(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    mood_key = call.data.replace("mood_", "")
    if mood_key not in MOODS:
        await call.answer("Неизвестный режим", show_alert=True)
        return
    chat_settings[str(call.message.chat.id)]["mood"] = mood_key
    save_settings()
    await call.message.edit_text(f"✅ Режим изменён на {MOODS[mood_key]['name']}", reply_markup=None)
    await call.answer()
    if MENU_DELETE_SECONDS > 0:
        asyncio.create_task(delete_message_later(call.message.chat.id, call.message.message_id, MENU_DELETE_SECONDS))

@router.callback_query(F.data == "settings")
async def show_settings(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    text, kb = settings_keyboard(call.message.chat.id)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data == "activity_menu")
async def show_activity_menu(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    await call.message.edit_text("📊 <b>Выбери точную активность без обращения</b>\n\n0% — бот молчит без прямого обращения.\n10% — реагирует редко.\n100% — реагирует почти всегда.", reply_markup=activity_keyboard())
    await call.answer()

@router.callback_query(F.data.startswith("set_activity_"))
async def set_activity(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    key = call.data.replace("set_activity_", "")
    if key not in ACTIVITY_LEVELS:
        await call.answer("Неизвестная активность", show_alert=True)
        return
    chat_settings[str(call.message.chat.id)]["activity_level"] = ACTIVITY_LEVELS[key]["value"]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_length")
async def toggle_length(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    cid = str(call.message.chat.id)
    lengths = list(RESPONSE_LENGTHS.keys())
    current = lengths.index(chat_settings[cid].get("response_length", "medium"))
    chat_settings[cid]["response_length"] = lengths[(current + 1) % len(lengths)]
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_request_only")
async def toggle_request_only(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["request_only"] = not s.get("request_only", False)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_sleep")
async def toggle_sleep(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["sleep_mode"] = not s.get("sleep_mode", False)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_reactions")
async def toggle_reactions(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["context_reactions"] = not s.get("context_reactions", True)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_stickers")
async def toggle_stickers(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["stickers_enabled"] = not s.get("stickers_enabled", True)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_profanity")
async def toggle_profanity(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["allow_profanity"] = not s.get("allow_profanity", False)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_morning")
async def toggle_morning(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["morning_enabled"] = not s.get("morning_enabled", True)
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "toggle_summary_schedule")
async def toggle_summary_schedule(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    s = chat_settings[str(call.message.chat.id)]
    s["summary_schedule_enabled"] = not s.get("summary_schedule_enabled", False)
    s["last_summary_sent_ts"] = datetime.datetime.now().timestamp()
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "summary_interval_menu")
async def summary_interval_menu(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    await call.message.edit_text("⏱ <b>Выбери интервал авто-summary</b>", reply_markup=summary_interval_keyboard())
    await call.answer()

@router.callback_query(F.data.startswith("set_summary_interval_"))
async def set_summary_interval(call: CallbackQuery):
    if not await require_admin_callback(call):
        return
    hours = int(call.data.replace("set_summary_interval_", ""))
    chat_settings[str(call.message.chat.id)]["summary_interval_hours"] = hours
    save_settings()
    await show_settings(call)

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(call: CallbackQuery):
    await call.message.edit_text("🎭 <b>Меню Оптимиста</b>", reply_markup=main_menu_keyboard())
    await call.answer()

@router.callback_query(F.data == "summary_menu")
async def show_summary_menu(call: CallbackQuery):
    await call.message.edit_text("📝 <b>За какой период сделать резюме?</b>", reply_markup=summary_keyboard())
    await call.answer()

@router.callback_query(F.data.startswith("summary_"))
async def summary_callback(call: CallbackQuery):
    hours = int(call.data.replace("summary_", ""))
    await call.answer("Составляю резюме...")
    summary = await build_summary(call.message.chat.id, hours)
    await call.message.edit_text(summary, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Периоды", callback_data="summary_menu"), InlineKeyboardButton(text="❌ Закрыть", callback_data="close_menu")]
    ]))

# ==================== КОМАНДЫ ====================
@router.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.reply(HELP_TEXT)

@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    cid = str(message.chat.id)
    stats = chat_stats[cid]
    today = datetime.date.today().isoformat()
    activity = get_activity_name(float(chat_settings[cid].get("activity_level", 0.1)))
    text = (
        "📊 <b>Статистика чата</b>\n\n"
        f"Всего сообщений: <b>{stats.get('total_messages', 0)}</b>\n"
        f"Участников: <b>{len(stats.get('participants', []))}</b>\n"
        f"Сегодня: <b>{stats.get('daily_messages', {}).get(today, 0)}</b>\n"
        f"Активность без обращения: <b>{activity}</b>\n"
        f"Сохранено сообщений для summary: <b>{len(stats.get('messages', []))}</b>\n"
        f"Стикеров запомнено: <b>{len(chat_stickers[cid])}</b>"
    )
    await message.reply(text)

# ==================== SUMMARY И ANALYZE ====================
async def build_summary(chat_id: int, hours: int) -> str:
    messages = get_recent_messages(chat_id, hours=hours, limit=400)
    if not messages:
        return f"📝 За последние {hours} ч сообщений для резюме нет."

    context = format_messages_for_llm(messages)
    system = (
        "Ты нейтральный аналитик группового чата. НЕ используй стиль текущего режима бота. "
        "Сделай краткое изложение обсуждения: главные темы, решения, вопросы без ответа, важные договорённости. "
        "Не придумывай факты. Если сообщений мало — так и скажи. Форматируй списком."
    )
    prompt = f"Сообщения за последние {hours} часов:\n{context}"
    summary = await ask_llm(system, prompt, max_tokens=650, temperature=0.4)
    if not summary:
        summary = "Не удалось получить ответ от ИИ. Попробуй позже."
    return f"📝 <b>Резюме за {hours} ч</b>\n\n{html_escape(summary)}"

@router.message(Command("summary"))
async def cmd_summary(message: types.Message):
    hours = parse_hours_arg(message.text, default=1)
    await message.reply("📝 Собираю резюме обсуждения...")
    await message.reply(await build_summary(message.chat.id, hours))

@router.message(Command("analyze", "анализ"))
async def cmd_analyze(message: types.Message):
    messages = get_recent_messages(message.chat.id, hours=24, limit=30)
    if not messages:
        await message.reply("🔪 Сообщений мало. Ведущий пока не видит, кто мафия.")
        return

    context = format_messages_for_llm(messages)
    system = (
        "Ты ведущий классической игры в Мафию и старый дон-наблюдатель. "
        "Это НЕ обычное резюме. Проанализируй поведение игроков по сообщениям. "
        "Найди активных, молчунов, подозрительных, тех, кого стоит проверить шерифу. "
        "Дай вердикт: 'Чисто' или 'Пахнет мафией'. Пиши в стиле ведущего Мафии, с юмором, но без реальных обвинений."
    )
    prompt = f"Последние сообщения чата:\n{context}\n\nСделай мафиозный анализ как ведущий игры."
    analysis = await ask_llm(system, prompt, max_tokens=650, temperature=0.7)
    if not analysis:
        analysis = "Стол молчит, ночь темна. Но я бы проверил самого спокойного — слишком уж чисто выглядит. 🕴️"
    await message.reply(f"🔪 <b>Мафиозный анализ</b>\n\n{html_escape(analysis)}")

# ==================== ГОРОСКОП ====================
ZODIAC_SIGNS = {
    "овен": "Овен", "телец": "Телец", "близнецы": "Близнецы", "рак": "Рак",
    "лев": "Лев", "дева": "Дева", "весы": "Весы", "скорпион": "Скорпион",
    "стрелец": "Стрелец", "козерог": "Козерог", "водолей": "Водолей", "рыбы": "Рыбы",
}

async def generate_horoscope(chat_id: int, user_name: str, sign: Optional[str] = None) -> str:
    cid = str(chat_id)
    today = datetime.date.today().isoformat()
    cache_key = sign if sign else "общий"
    cache = chat_settings[cid].get("horoscope_cache", {})
    if cache.get("date") == today and cache.get(cache_key):
        return cache[cache_key]

    mood = MOODS.get(chat_settings[cid].get("mood", "optimist"), MOODS["optimist"])["name"]
    target = f"для знака {sign}" if sign else f"для {user_name}"
    prompt = f"Напиши короткий позитивный гороскоп на сегодня {target}. Тон: {mood}. 5-7 предложений с эмодзи."
    system = "Ты астролог-оптимист. Отвечай сразу, без вступления и без дисклеймеров."
    text = await ask_llm(system, prompt, max_tokens=300, temperature=0.9)
    if not text:
        text = f"@{user_name}, сегодня звёзды говорят: удача любит тех, кто делает первый шаг. 🌟"

    if "horoscope_cache" not in chat_settings[cid] or not isinstance(chat_settings[cid]["horoscope_cache"], dict):
        chat_settings[cid]["horoscope_cache"] = {}
    chat_settings[cid]["horoscope_cache"]["date"] = today
    chat_settings[cid]["horoscope_cache"][cache_key] = text
    save_settings()
    return text

@router.message(Command("horoscope", "гороскоп"))
async def cmd_horoscope(message: types.Message):
    user_name = message.from_user.first_name or "друг"
    parts = message.text.strip().split()
    sign = None
    if len(parts) > 1:
        raw = parts[1].lower().strip()
        for key, name in ZODIAC_SIGNS.items():
            if key == raw or key.startswith(raw) or raw.startswith(key[:4]):
                sign = name
                break
    await message.reply("🔮 Генерирую гороскоп...")
    text = await generate_horoscope(message.chat.id, user_name, sign)
    if sign:
        await message.reply(f"🔮 <b>{sign}</b>\n{text}")
    else:
        await message.reply(text)

# ==================== РИСОВАНИЕ ====================
@router.message(F.text.lower().regexp(r"^(нарисуй|сделай картинку|создай картинку|создай изображение|сгенерируй изображение|сгенерируй картинку|сгенерируй стикер|покажи картинку)"))
async def cmd_draw(message: types.Message):
    prompt, style, sticker_mode = parse_draw_prompt(message.text or "")
    if not prompt or len(prompt) < 2:
        await message.reply("🖼️ Что нарисовать? Пример: <code>нарисуй кота в стиле киберпанк</code>")
        return

    await message.reply(f"🎨 Рисую: <b>{html_escape(prompt)}</b>")
    image_bytes = await fusionbrain_generate_image(prompt, style)
    if image_bytes:
        try:
            img = BufferedInputFile(image_bytes, filename="optimist_image.png")
            await bot.send_photo(message.chat.id, img, caption=f"✨ {html_escape(prompt)}")
            return
        except Exception as e:
            logger.warning(f"Ошибка отправки FusionBrain image: {e}")

    try:
        url = await pollinations_image_url(prompt, style)
        await bot.send_photo(message.chat.id, url, caption=f"✨ {html_escape(prompt)}")
    except Exception as e:
        logger.error(f"Ошибка генерации/отправки картинки: {e}")
        await message.reply("😔 Не получилось сгенерировать изображение. Попробуй другой запрос.")

# ==================== СТИКЕРЫ ====================
@router.message(F.sticker)
async def handle_sticker(message: types.Message):
    if message.sticker:
        remember_sticker(message.chat.id, message.sticker)

# ==================== СОН И ТИШИНА ====================
def parse_silence_minutes(text: str) -> int:
    m = re.search(r"(\d{1,3})", text)
    if m:
        return max(1, min(int(m.group(1)), 1440))
    return 15


def is_sleep_command(text: str) -> bool:
    lower = text.lower().strip()
    return bool(re.search(r"\bоптимист\b.*\b(спи|спать|усни|засыпай)\b", lower))


def is_wake_command(text: str) -> bool:
    lower = text.lower().strip()
    return bool(re.search(r"\bоптимист\b.*\b(проснись|вставай|пробуждайся|хватит спать)\b", lower))

# ==================== ОСНОВНОЙ ОБРАБОТЧИК ====================
@router.message()
async def main_handler(message: types.Message):
    if not message.text:
        return
    if message.text.startswith("/"):
        return

    chat_id = message.chat.id
    cid = str(chat_id)
    user_name = message.from_user.first_name or "друг"
    text = message.text.strip()
    lower = text.lower()
    s = chat_settings[cid]

    update_chat_stats(chat_id, message.from_user.id, text, user_name=user_name)

    if any(trigger in lower for trigger in ABOUT_TRIGGERS):
        await message.reply(ABOUT_BOT_TEXT)
        return

    # Режим сна: включается командой и полностью молчит до обращения.
    if is_sleep_command(text):
        s["sleep_mode"] = True
        save_settings()
        await message.reply("😴 Оптимист уходит в сон. Разбудишь меня обращением: <b>Оптимист, проснись</b>.")
        return

    if is_wake_command(text):
        s["sleep_mode"] = False
        save_settings()
        await message.reply("🌅 Я проснулся. Дон, инвестор и юморист внутри тоже потянулись.")
        return

    direct = is_direct_request(message)

    if s.get("sleep_mode", False) and not direct:
        return
    if s.get("sleep_mode", False) and direct:
        s["sleep_mode"] = False
        save_settings()

    # Обычная временная тишина.
    if float(s.get("silence_until", 0.0)) > datetime.datetime.now().timestamp() and not direct:
        return
    if any(word in lower for word in ["помолчи", "тихо", "молчи"]):
        minutes = parse_silence_minutes(lower)
        s["silence_until"] = datetime.datetime.now().timestamp() + minutes * 60
        save_settings()
        await message.reply(f"🤫 Молчу {minutes} мин.")
        return

    # Группа: режим только по запросу полностью запрещает самовольную активность.
    if chat_id < 0 and not direct:
        if s.get("request_only", False):
            return

        # Один roll на всё поведение. 10% = максимум 10% любых реакций/стикеров/ответов без обращения.
        if random() >= float(s.get("activity_level", 0.1)):
            return

        kind = detect_context_kind(text)
        action_roll = random()
        if s.get("context_reactions", True) and action_roll < 0.65:
            await set_context_reaction(message, kind)
            return
        if s.get("stickers_enabled", True) and action_roll < 0.85:
            sent = await send_context_sticker(message, kind)
            if sent:
                return
        # Редкий короткий ответ только в рамках общей интенсивности.

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    response = await get_llm_response(text, chat_id, user_name)
    try:
        await message.reply(response)
    except Exception as e:
        logger.error(f"reply error: {e}")

# ==================== КУРСЫ И УТРЕННЕЕ ПРИВЕТСТВИЕ ====================
async def fetch_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> Optional[dict]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status == 200:
                return await resp.json()
            logger.warning(f"GET {url} -> {resp.status}")
    except Exception as e:
        logger.warning(f"GET {url} error: {e}")
    return None


def fmt_money(value: Any, digits: int = 2) -> str:
    try:
        num = float(value)
        if num >= 1000:
            return f"{num:,.0f}".replace(",", " ")
        return f"{num:.{digits}f}".replace(".", ",")
    except Exception:
        return "н/д"


async def get_rates() -> Dict[str, Any]:
    rates: Dict[str, Any] = {"btc": None, "usdt": None, "usd": None, "eur": None, "source": []}
    async with aiohttp.ClientSession() as session:
        # 1) ЦБ РФ для USD/EUR.
        cbr = await fetch_json(session, "https://www.cbr-xml-daily.ru/daily_json.js")
        if cbr and cbr.get("Valute"):
            try:
                rates["usd"] = float(cbr["Valute"]["USD"]["Value"])
                rates["eur"] = float(cbr["Valute"]["EUR"]["Value"])
                rates["source"].append("ЦБ РФ")
            except Exception as e:
                logger.warning(f"CBR parse error: {e}")

        # 2) CoinGecko для BTC/USDT в RUB.
        cg = await fetch_json(session, "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=rub")
        if cg:
            try:
                if cg.get("bitcoin", {}).get("rub"):
                    rates["btc"] = float(cg["bitcoin"]["rub"])
                if cg.get("tether", {}).get("rub"):
                    rates["usdt"] = float(cg["tether"]["rub"])
                rates["source"].append("CoinGecko")
            except Exception as e:
                logger.warning(f"CoinGecko parse error: {e}")

        # 3) Binance BTCUSDT + USD/RUB как fallback для BTC.
        if rates["btc"] is None and rates["usd"]:
            binance = await fetch_json(session, "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
            try:
                if binance and binance.get("price"):
                    rates["btc"] = float(binance["price"]) * float(rates["usd"])
                    rates["source"].append("Binance×ЦБ")
            except Exception as e:
                logger.warning(f"Binance parse error: {e}")

        # 4) USDT fallback примерно равен USD/RUB.
        if rates["usdt"] is None and rates["usd"] is not None:
            rates["usdt"] = rates["usd"]
            rates["source"].append("USDT≈USD")

    return rates


async def send_morning_greeting(chat_id: int):
    quote = choice(OPTIMISTIC_QUOTES)
    rates = await get_rates()
    source = ", ".join(dict.fromkeys(rates.get("source", []))) or "источники временно недоступны"
    if all(rates.get(k) is None for k in ["btc", "usdt", "usd", "eur"]):
        rates_text = "💰 <b>Курсы:</b> временно не удалось получить данные."
    else:
        rates_text = (
            "💰 <b>Курсы:</b>\n"
            f"• BTC: <b>{fmt_money(rates.get('btc'), 0)}</b> ₽\n"
            f"• USDT: <b>{fmt_money(rates.get('usdt'))}</b> ₽\n"
            f"• USD: <b>{fmt_money(rates.get('usd'))}</b> ₽\n"
            f"• EUR: <b>{fmt_money(rates.get('eur'))}</b> ₽\n"
            f"<i>Источник: {html_escape(source)}</i>"
        )
    text = f"🌅 <b>Доброе утро!</b>\n\n{quote}\n\n{rates_text}"
    try:
        await bot.send_message(chat_id, text)
    except Exception as e:
        logger.error(f"Утреннее сообщение ошибка: {e}")


async def morning_loop():
    tz = ZoneInfo(MORNING_TZ)
    while True:
        try:
            now = datetime.datetime.now(tz)
            if now.hour == MORNING_HOUR and now.minute < 5:
                today = now.date().isoformat()
                for cid_str, s in list(chat_settings.items()):
                    if s.get("morning_enabled", True) and s.get("last_morning_sent") != today:
                        await send_morning_greeting(int(cid_str))
                        s["last_morning_sent"] = today
                        save_settings()
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Morning loop error: {e}")
            await asyncio.sleep(60)


async def summary_schedule_loop():
    while True:
        try:
            now_ts = datetime.datetime.now().timestamp()
            for cid_str, s in list(chat_settings.items()):
                if not s.get("summary_schedule_enabled", False):
                    continue
                interval = int(s.get("summary_interval_hours", 6))
                last_ts = float(s.get("last_summary_sent_ts", 0.0))
                if now_ts - last_ts < interval * 3600:
                    continue
                messages = get_recent_messages(int(cid_str), hours=interval, limit=400)
                if len(messages) < 3:
                    s["last_summary_sent_ts"] = now_ts
                    save_settings()
                    continue
                summary = await build_summary(int(cid_str), interval)
                await bot.send_message(int(cid_str), summary)
                s["last_summary_sent_ts"] = now_ts
                save_settings()
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Summary loop error: {e}")
            await asyncio.sleep(300)

# ==================== ПРИВЕТСТВИЕ ПРИ ДОБАВЛЕНИИ ====================
@router.message(F.new_chat_members)
async def welcome_new_chat(message: types.Message):
    for user in message.new_chat_members:
        if BOT_ID and user.id == BOT_ID:
            await message.reply(
                "🤖 <b>Привет! Я Оптимист.</b>\n"
                "Умею отвечать в разных стилях, рисовать, делать /summary и мафиозный /analyze.\n"
                "Открой /menu, чтобы настроить меня."
            )
            break

# ==================== ЗАПУСК ====================
async def on_startup():
    global BOT_USERNAME, BOT_ID
    load_settings()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    BOT_ID = me.id

    commands = [
        BotCommand(command="start", description="Приветствие и меню"),
        BotCommand(command="menu", description="Меню режимов и настроек"),
        BotCommand(command="help", description="Список команд"),
        BotCommand(command="stats", description="Статистика чата"),
        BotCommand(command="summary", description="Резюме чата за N часов"),
        BotCommand(command="analyze", description="Мафиозный анализ чата"),
        BotCommand(command="horoscope", description="Гороскоп на сегодня"),
    ]
    await bot.set_my_commands(commands)

    logger.info(f"🚀 Бот @{BOT_USERNAME} запущен")
    asyncio.create_task(morning_loop())
    asyncio.create_task(summary_schedule_loop())


async def main():
    dp.include_router(router)
    await on_startup()
    threading.Thread(target=start_http_server, daemon=True).start()
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
