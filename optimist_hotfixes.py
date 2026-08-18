#!/usr/bin/env python3
"""Runtime hotfixes for Optimist Bot.

Keeps the original monolithic bot untouched while fixing production issues:
- adaptive, context-aware normal chat replies without topic leakage;
- natural-language image intents such as "Оптимист нарисуй ...";
- current image generation fallbacks for Pixazo / Gemini / Pollinations;
- cleaner /missed formatting.
"""

import html
import json
import os
import re
import time
import urllib.parse
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile


SMART_CONTEXT_MAX_MESSAGES = max(10, int(os.getenv("SMART_CONTEXT_MAX_MESSAGES", "24")))
SMART_CONTEXT_MAX_CHARS = max(4000, int(os.getenv("SMART_CONTEXT_MAX_CHARS", "12000")))
AI_RESPONSE_TEMPERATURE = min(1.0, max(0.0, float(os.getenv("AI_RESPONSE_TEMPERATURE", "0.58"))))
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_BASE_URL = os.getenv("POLLINATIONS_BASE_URL", "https://gen.pollinations.ai").rstrip("/")
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip() or "flux"

IMAGE_DIAGNOSTICS: Dict[str, Any] = {
    "last_provider": "none",
    "last_error": "",
    "last_ts": 0.0,
}

_ORIGINAL_GET_LLM_RESPONSE = None
_RUNTIME_ROUTER_INSTALLED = False

_DRAW_RE = re.compile(
    r"^\s*(?:@\w+\s+)?(?:(?:оптимист|бот)(?:\s+|[,!:;\-]+\s*))?"
    r"(?P<cmd>нарисуй|сделай\s+картинку|создай\s+картинку|создай\s+изображение|"
    r"сгенерируй\s+изображение|сгенерируй\s+картинку|сгенерируй\s+стикер|покажи\s+картинку)\b",
    re.IGNORECASE,
)

_RESET_PATTERNS = [
    re.compile(r"^(?:привет|приветик|здравствуй|здравствуйте|здорово|хай|hello|hi)[!?.\s]*$", re.I),
    re.compile(r"^(?:привет\s+)?(?:ты\s+)?оптимист[!?.\s]*$", re.I),
    re.compile(r"^(?:как\s+ты|как\s+дела|ты\s+как)[!?.\s]*$", re.I),
    re.compile(r"^(?:о[,\s]+)?(?:жив|живой|ты\s+жив|ты\s+тут|на\s+месте)[!?.\s]*$", re.I),
    re.compile(r"^(?:кто\s+ты|что\s+ты\s+умеешь)[!?.\s]*$", re.I),
]

_FOLLOWUP_RE = re.compile(
    r"^(?:почему|зачем|а\s+если|и\s+что|а\s+что|тогда|а\s+как|и\s+как|"
    r"а\s+он|а\s+она|а\s+они|а\s+это|и\s+это|это|он|она|они|да|нет|точно|"
    r"серьезно|серьёзно|правда|дальше|продолжай|и\?|а\?)\b",
    re.I,
)


def _set_image_diag(provider: str, error: str = "") -> None:
    IMAGE_DIAGNOSTICS["last_provider"] = provider
    IMAGE_DIAGNOSTICS["last_error"] = (error or "")[:240]
    IMAGE_DIAGNOSTICS["last_ts"] = time.time()


def image_diag_text() -> str:
    provider = str(IMAGE_DIAGNOSTICS.get("last_provider") or "none")
    error = str(IMAGE_DIAGNOSTICS.get("last_error") or "")
    if error:
        return f"{provider}: {error}"
    return provider


def _pixazo_payload(prompt: str) -> Dict[str, Any]:
    return {
        "prompt": prompt,
        "steps": 25,
        "width": 1024,
        "height": 1024,
    }


def _pollinations_url(prompt: str) -> str:
    encoded = urllib.parse.quote(prompt, safe="")
    return (
        f"{POLLINATIONS_BASE_URL}/image/{encoded}"
        f"?model={urllib.parse.quote(POLLINATIONS_IMAGE_MODEL, safe='')}"
        "&width=1024&height=1024"
    )


def _clean_addressing(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^@\w+\s*[,!:;\-]?\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:оптимист|бот)\s*[,!:;\-]?\s*", "", value, flags=re.I)
    return value.strip()


def context_mode(text: str, current_item: Optional[Dict[str, Any]] = None) -> str:
    """Return reset/followup/normal for adaptive dialogue memory."""
    cleaned = _clean_addressing(text).lower().strip()
    if any(pattern.fullmatch(cleaned) for pattern in _RESET_PATTERNS):
        return "reset"
    if current_item and current_item.get("reply_to_user_id"):
        return "followup"
    words = re.findall(r"[а-яёa-z0-9]+", cleaned, flags=re.I)
    if _FOLLOWUP_RE.search(cleaned):
        return "followup"
    if len(words) <= 5 and any(token in cleaned for token in ["это", "он", "она", "они", "там", "тогда"]):
        return "followup"
    return "normal"


def normalize_draw_text(text: str) -> Optional[str]:
    """Strip optional bot addressing and return text beginning at the draw verb."""
    match = _DRAW_RE.search(text or "")
    if not match:
        return None
    return (text or "")[match.start("cmd"):].strip()


def build_smart_context(messages: List[Dict[str, Any]], max_chars: int = SMART_CONTEXT_MAX_CHARS) -> str:
    lines: List[str] = []
    used = 0
    for item in reversed(messages):
        text = (item.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        author = (item.get("user") or item.get("username") or "участник").strip()
        reply_to = (item.get("reply_to_user") or "").strip()
        prefix = f"{author} → {reply_to}" if reply_to else author
        line = f"{prefix}: {text[:1000]}"
        cost = len(line) + 1
        if lines and used + cost > max_chars:
            break
        lines.append(line)
        used += cost
    lines.reverse()
    return "\n".join(lines) if lines else "Контекста нет."


def _select_context(prior: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    if mode == "reset":
        return []
    if mode == "followup":
        return prior[-14:]
    return prior[-7:]


def _fallback_response(user_name: str, user_text: str, mood_key: str) -> str:
    clean = _clean_addressing(user_text).lower()
    prefix = f"@{user_name}"
    if re.search(r"\bкак\s+(?:ты|дела)\b", clean):
        return f"{prefix} Отлично 😄 Живой, бодрый и снова в строю."
    if any(word in clean for word in ["привет", "здравств", "хай", "hello"]):
        return f"{prefix} Привет! Я на месте 😄"
    if any(phrase in clean for phrase in ["жив", "ты тут", "на месте"]):
        return f"{prefix} Ага, жив 😄 И уже нормально соображаю."
    if mood_key == "pessimist":
        return f"{prefix} Я тут, но AI-провайдер сейчас не ответил. Лучше повтори через пару секунд."
    return f"{prefix} Я на месте. AI-провайдер сейчас не дал ответ — повтори через пару секунд."


async def smart_get_llm_response(app, user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    settings = app.chat_settings[cid]
    mood_key = settings.get("mood", "optimist")

    if mood_key == "investor_genius" and _ORIGINAL_GET_LLM_RESPONSE is not None:
        return await _ORIGINAL_GET_LLM_RESPONSE(user_text, chat_id, user_name)

    mood = app.MOODS.get(mood_key, app.MOODS["optimist"])
    length = app.RESPONSE_LENGTHS.get(
        settings.get("response_length", "medium"),
        app.RESPONSE_LENGTHS["medium"],
    )
    allow_prof = settings.get("allow_profanity", False)
    recent = app.get_recent_messages(chat_id, limit=SMART_CONTEXT_MAX_MESSAGES)
    current_item = recent[-1] if recent else None
    prior = recent[:-1] if recent else []
    mode = context_mode(user_text, current_item)
    selected = _select_context(prior, mode)
    context = build_smart_context(selected)
    cleaned = app.clean_user_text_for_llm(user_text)
    prof_rule = (
        "Мат можно использовать умеренно, только если он действительно уместен."
        if allow_prof
        else "Мат, грубость и оскорбления запрещены."
    )

    mode_rule = {
        "reset": (
            "Это самостоятельная реплика или новая тема. НЕ продолжай прошлую тему и не упоминай старый контекст. "
            "На приветствия, 'как ты?' и подобный small talk отвечай естественно, коротко и по-человечески — без советов и планов."
        ),
        "followup": (
            "Это продолжение разговора. Используй ближайший контекст, чтобы точно восстановить, к чему относятся местоимения, "
            "короткие вопросы и фразы вроде 'почему?', 'а если?', 'и что?'."
        ),
        "normal": (
            "Это обычная реплика. История — только справка: используй её лишь когда она прямо помогает понять текущий смысл. "
            "Если текущая реплика начинает новую тему, игнорируй старую историю."
        ),
    }[mode]

    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length['rule']}\n"
        f"{prof_rule}\n"
        "Ты умный живой участник Telegram-чата, а не шаблонный помощник.\n"
        f"Контекстный режим: {mode}. {mode_rule}\n"
        "Главное — понять смысл текущей реплики. Не цепляйся за прошлую тему только потому, что она есть в истории.\n"
        "Ответ должен быть логичным, конкретным и естественным. Не добавляй мотивационную воду, лишние советы, планы или объяснения, если их не просили.\n"
        "Не выдумывай факты. Не обещай сделать то, чего бот в этом обработчике фактически не делает.\n"
        f"Начинай ответ с @{user_name}. Не повторяй дословно сообщение пользователя и не пиши 'ты спросил' или 'по поводу'.\n\n"
        f"Релевантная история:\n{context}"
    )

    temperature = 0.68 if mood_key == "humor" else AI_RESPONSE_TEMPERATURE
    answer = await app.ask_llm(
        system_prompt,
        cleaned,
        length["max_tokens"],
        temperature=temperature,
    )
    if answer:
        return answer
    return _fallback_response(user_name, user_text, mood_key)


async def _download_image(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None) -> Optional[bytes]:
    try:
        async with session.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            raw = await resp.read()
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if resp.status == 200 and content_type.startswith("image/") and raw:
                return raw
            return None
    except Exception:
        return None


async def pixazo_generate_image_bytes(app, prompt: str, style: str = "") -> Optional[bytes]:
    if not app.PIXAZO_API_KEY:
        return None
    final_prompt = f"{prompt}, {style or 'high quality'}"
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Ocp-Apim-Subscription-Key": app.PIXAZO_API_KEY,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                app.PIXAZO_ENDPOINT,
                json=_pixazo_payload(final_prompt),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=app.PIXAZO_TIMEOUT_SECONDS),
            ) as resp:
                raw = await resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if resp.status != 200:
                    _set_image_diag("Pixazo", f"HTTP {resp.status}")
                    app.logger.warning("Pixazo image status %s: %r", resp.status, raw[:300])
                    return None
                if content_type.startswith("image/"):
                    _set_image_diag(f"Pixazo/{app.PIXAZO_MODEL}")
                    return raw
                try:
                    data = json.loads(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    _set_image_diag("Pixazo", "invalid JSON response")
                    return None
                url = app.find_first_url(data)
                if not url:
                    _set_image_diag("Pixazo", "response without image URL")
                    return None
                image = await _download_image(session, url)
                if image:
                    _set_image_diag(f"Pixazo/{app.PIXAZO_MODEL}")
                    return image
                _set_image_diag("Pixazo", "could not download output image")
    except Exception as exc:
        _set_image_diag("Pixazo", str(exc))
        app.logger.warning("Pixazo image hotfix error: %s", exc)
    return None


async def pollinations_generate_image_bytes(app, prompt: str, style: str = "") -> Optional[bytes]:
    if not POLLINATIONS_API_KEY:
        return None
    full_prompt = f"{prompt}, {style or 'high quality, detailed'}"
    url = _pollinations_url(full_prompt)
    headers = {"Authorization": f"Bearer {POLLINATIONS_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                raw = await resp.read()
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if resp.status == 200 and content_type.startswith("image/") and raw:
                    _set_image_diag(f"Pollinations/{POLLINATIONS_IMAGE_MODEL}")
                    return raw
                body = raw[:220].decode("utf-8", errors="ignore")
                _set_image_diag("Pollinations", f"HTTP {resp.status}: {body}")
                app.logger.warning("Pollinations image status %s: %r", resp.status, raw[:300])
    except Exception as exc:
        _set_image_diag("Pollinations", str(exc))
        app.logger.warning("Pollinations image error: %s", exc)
    return None


async def generate_image(app, prompt: str, style: str = "", sticker_mode: bool = False) -> Tuple[Optional[bytes], Optional[str], str]:
    improved_prompt = await app.enhance_image_prompt(prompt, style, sticker_mode)

    providers: List[str] = []
    if app.PIXAZO_API_KEY:
        providers.append("pixazo")
    if app.GEMINI_API_KEY:
        providers.append("gemini")
    if app.HF_TOKEN:
        providers.append("huggingface")
    if POLLINATIONS_API_KEY:
        providers.append("pollinations")
    providers.append("horde")

    for provider in providers:
        if provider == "pixazo":
            image = await pixazo_generate_image_bytes(app, improved_prompt, style)
            if image:
                return image, None, f"Pixazo/{app.PIXAZO_MODEL}"
        elif provider == "gemini":
            image = await app.gemini_generate_image_bytes(improved_prompt, style)
            if image:
                _set_image_diag(f"Gemini/{app.GEMINI_IMAGE_MODEL}")
                return image, None, f"Gemini/{app.GEMINI_IMAGE_MODEL}"
        elif provider == "huggingface":
            image = await app.huggingface_generate_image_bytes(improved_prompt, style)
            if image:
                _set_image_diag(f"HuggingFace/{app.HF_IMAGE_MODEL}")
                return image, None, f"HuggingFace/{app.HF_IMAGE_MODEL}"
        elif provider == "pollinations":
            image = await pollinations_generate_image_bytes(app, improved_prompt, style)
            if image:
                return image, None, f"Pollinations/{POLLINATIONS_IMAGE_MODEL}"
        elif provider == "horde":
            image = await app.ai_horde_generate_image_bytes(improved_prompt, style)
            if image:
                _set_image_diag(f"AI Horde/{app.AI_HORDE_MODEL}")
                return image, None, f"AI Horde/{app.AI_HORDE_MODEL}"

    if not IMAGE_DIAGNOSTICS.get("last_error"):
        _set_image_diag("none", "no configured image provider returned an image")
    return None, None, "none"


def _telegram_html_from_llm(text: str) -> str:
    escaped = html.escape((text or "").strip())
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.S)
    escaped = re.sub(r"(?m)^#{1,4}\s*", "", escaped)
    return escaped


def _recent_messages_for_hours(app, chat_id: int, hours: int) -> List[Dict[str, Any]]:
    cutoff = time.time() - hours * 3600
    messages = app.chat_stats[str(chat_id)].get("messages", [])
    return [m for m in messages if float(m.get("ts", 0)) >= cutoff]


def build_runtime_router(app) -> Router:
    router = Router(name="optimist_runtime_intents")

    @router.message(F.text.func(lambda text: bool(normalize_draw_text(text or ""))))
    async def natural_draw(message: types.Message):
        normalized = normalize_draw_text(message.text or "")
        if not normalized:
            return
        prompt, style, sticker_mode = app.parse_draw_prompt(normalized)
        if not prompt or len(prompt) < 2:
            await message.reply("🖼️ Что нарисовать? Например: <code>Оптимист, нарисуй кота в киберпанке</code>")
            return

        status = await message.reply(f"🎨 Рисую: <b>{html.escape(prompt)}</b>")
        image_bytes, image_url, provider = await app.generate_image(prompt, style, sticker_mode=sticker_mode)
        if image_bytes:
            try:
                img = BufferedInputFile(image_bytes, filename="optimist_image.png")
                await app.bot.send_photo(message.chat.id, img, caption=f"✨ {html.escape(prompt)}\n<i>{html.escape(provider)}</i>")
                try:
                    await status.delete()
                except Exception:
                    pass
                return
            except Exception as exc:
                app.logger.warning("runtime image send error: %s", exc)
        if image_url:
            try:
                await app.bot.send_photo(message.chat.id, image_url, caption=f"✨ {html.escape(prompt)}\n<i>{html.escape(provider)}</i>")
                try:
                    await status.delete()
                except Exception:
                    pass
                return
            except Exception as exc:
                app.logger.warning("runtime image URL send error: %s", exc)
        try:
            await status.edit_text(
                "😔 Генератор изображения не ответил. Отправь <code>/status</code> — там будет последняя ошибка image-provider."
            )
        except Exception:
            await message.reply("😔 Генератор изображения не ответил. Проверь <code>/status</code>.")

    @router.message(Command("missed", "catchup"))
    async def clean_missed(message: types.Message):
        parts = (message.text or "").split()
        hours = 8
        if len(parts) > 1:
            try:
                hours = max(1, min(int(parts[1]), 168))
            except ValueError:
                await message.reply("Используй: <code>/missed 8</code> — число часов от 1 до 168.")
                return
        messages = _recent_messages_for_hours(app, message.chat.id, hours)
        if not messages:
            await message.reply(f"😴 За последние {hours} ч. сообщений для сводки нет.")
            return
        if len(messages) < 3:
            authors = Counter((m.get("user") or m.get("username") or "Участник") for m in messages)
            names = ", ".join(f"{html.escape(name)} — {count}" for name, count in authors.most_common(3))
            await message.reply(
                f"📰 <b>Что я пропустил за {hours} ч.</b>\n\n💬 Сообщений: <b>{len(messages)}</b>\n👥 {names}"
            )
            return

        transcript = "\n".join(
            f"{m.get('user') or m.get('username') or 'Участник'}: {(m.get('text') or '').replace(chr(10), ' ')[:500]}"
            for m in messages[-160:]
            if (m.get("text") or "").strip()
        )[-24000:]
        system = (
            "Ты ведущий Telegram-группы. Сделай короткий фактический catch-up. Не выдумывай. "
            "Не используй Markdown-звёздочки, Markdown-заголовки или кодовые блоки; верни обычный текст с эмодзи и переносами строк."
        )
        prompt = (
            "Структура: Главная тема; яркий/смешной момент, если был; спор или напряжение, если было; "
            "самый заметный участник и почему; короткий финал в стиле Оптимиста.\n\nСООБЩЕНИЯ:\n" + transcript
        )
        status = await message.reply(f"📰 Собираю, что ты пропустил за <b>{hours} ч.</b>...")
        result = await app.ask_llm(system, prompt, max_tokens=600, temperature=0.45)
        if result:
            final_text = f"📰 <b>Что я пропустил за {hours} ч.</b>\n\n{_telegram_html_from_llm(result)}"
        else:
            final_text = f"📰 <b>Что я пропустил за {hours} ч.</b>\n\nAI сейчас не ответил, но сохранено сообщений: <b>{len(messages)}</b>."
        try:
            await status.edit_text(final_text)
        except Exception:
            await message.reply(final_text)

    return router


def install(app) -> None:
    """Patch runtime lookup points and register intent handlers before the legacy router."""
    global _ORIGINAL_GET_LLM_RESPONSE, _RUNTIME_ROUTER_INSTALLED
    if _ORIGINAL_GET_LLM_RESPONSE is None:
        _ORIGINAL_GET_LLM_RESPONSE = app.get_llm_response

    async def _smart(user_text: str, chat_id: int, user_name: str) -> str:
        return await smart_get_llm_response(app, user_text, chat_id, user_name)

    async def _image(prompt: str, style: str = "", sticker_mode: bool = False):
        return await generate_image(app, prompt, style, sticker_mode)

    app.get_llm_response = _smart
    app.generate_image = _image

    if not _RUNTIME_ROUTER_INSTALLED:
        app.dp.include_router(build_runtime_router(app))
        _RUNTIME_ROUTER_INSTALLED = True

    app.logger.info(
        "🧠 AI hotfix installed: adaptive context temp=%.2f max=%s; natural draw intents ON; image providers Pixazo/Gemini/HF/Pollinations/Horde",
        AI_RESPONSE_TEMPERATURE,
        SMART_CONTEXT_MAX_MESSAGES,
    )
