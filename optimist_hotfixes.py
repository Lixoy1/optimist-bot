#!/usr/bin/env python3
"""Runtime hotfixes for Optimist Bot.

Keeps the original monolithic bot untouched while fixing two production issues:
- smarter, more context-aware normal chat replies;
- current image generation fallbacks for Pixazo / Gemini / Pollinations.
"""

import html
import json
import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import aiohttp


SMART_CONTEXT_MAX_MESSAGES = max(10, int(os.getenv("SMART_CONTEXT_MAX_MESSAGES", "40")))
SMART_CONTEXT_MAX_CHARS = max(4000, int(os.getenv("SMART_CONTEXT_MAX_CHARS", "16000")))
AI_RESPONSE_TEMPERATURE = min(1.0, max(0.0, float(os.getenv("AI_RESPONSE_TEMPERATURE", "0.62"))))
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
POLLINATIONS_BASE_URL = os.getenv("POLLINATIONS_BASE_URL", "https://gen.pollinations.ai").rstrip("/")
POLLINATIONS_IMAGE_MODEL = os.getenv("POLLINATIONS_IMAGE_MODEL", "flux").strip() or "flux"

IMAGE_DIAGNOSTICS: Dict[str, Any] = {
    "last_provider": "none",
    "last_error": "",
    "last_ts": 0.0,
}

_ORIGINAL_GET_LLM_RESPONSE = None


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
    """Exact documented Flux 2 Klein request body; avoids unsupported fields."""
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


def build_smart_context(messages: List[Dict[str, Any]], max_chars: int = SMART_CONTEXT_MAX_CHARS) -> str:
    """Build recent conversational context with reply relationships preserved."""
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
    return "\n".join(lines) if lines else "Контекста пока нет."


async def smart_get_llm_response(app, user_text: str, chat_id: int, user_name: str) -> str:
    cid = str(chat_id)
    settings = app.chat_settings[cid]
    mood_key = settings.get("mood", "optimist")

    # Preserve the specialized investor reasoning flow exactly as it was.
    if mood_key == "investor_genius" and _ORIGINAL_GET_LLM_RESPONSE is not None:
        return await _ORIGINAL_GET_LLM_RESPONSE(user_text, chat_id, user_name)

    mood = app.MOODS.get(mood_key, app.MOODS["optimist"])
    length = app.RESPONSE_LENGTHS.get(
        settings.get("response_length", "medium"),
        app.RESPONSE_LENGTHS["medium"],
    )
    allow_prof = settings.get("allow_profanity", False)
    recent = app.get_recent_messages(chat_id, limit=SMART_CONTEXT_MAX_MESSAGES)
    context_items = recent[:-1] if recent else []
    context = build_smart_context(context_items)
    cleaned = app.clean_user_text_for_llm(user_text)
    prof_rule = (
        "Мат можно использовать умеренно, если он действительно уместен."
        if allow_prof
        else "Мат, грубость и оскорбления запрещены."
    )

    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length['rule']}\n"
        f"{prof_rule}\n"
        "Ты ведёшь непрерывный живой диалог в Telegram, а не отвечаешь на каждое сообщение изолированно.\n"
        "Сначала восстанови смысл текущей реплики из ближайшего контекста: местоимения, 'это', 'он', 'она', "
        "короткие ответы вроде 'да', 'почему', 'а если', шутки и Reply должны связываться с предыдущими сообщениями.\n"
        "Не выдумывай факты, которых нет в сообщениях. Если контекста реально недостаточно — кратко скажи, чего не хватает.\n"
        "Ответ должен быть логичным и причинно-следственным: тезис → почему → конкретный вывод/следующий шаг, когда это уместно.\n"
        "Избегай мотивационной воды, шаблонных вступлений и общих фраз. Реагируй именно на содержание разговора.\n"
        f"Начинай ответ с @{user_name}. Не повторяй дословно сообщение пользователя и не пиши 'ты спросил' или 'по поводу'.\n\n"
        f"Контекст последних сообщений:\n{context}"
    )

    temperature = 0.70 if mood_key == "humor" else AI_RESPONSE_TEMPERATURE
    answer = await app.ask_llm(
        system_prompt,
        cleaned,
        length["max_tokens"],
        temperature=temperature,
    )
    if answer:
        return answer
    return app.local_fallback(user_name, mood_key)


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
    # Use the existing LLM prompt enhancer when available. Even if it fails, it
    # already has a local fallback and returns a usable prompt.
    improved_prompt = await app.enhance_image_prompt(prompt, style, sticker_mode)

    # Prefer providers that are actually configured. Gemini is now automatic;
    # previously it was never reached with the default provider list.
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


def install(app) -> None:
    """Patch only the runtime lookup points used by the original handlers."""
    global _ORIGINAL_GET_LLM_RESPONSE
    if _ORIGINAL_GET_LLM_RESPONSE is None:
        _ORIGINAL_GET_LLM_RESPONSE = app.get_llm_response

    async def _smart(user_text: str, chat_id: int, user_name: str) -> str:
        return await smart_get_llm_response(app, user_text, chat_id, user_name)

    async def _image(prompt: str, style: str = "", sticker_mode: bool = False):
        return await generate_image(app, prompt, style, sticker_mode)

    app.get_llm_response = _smart
    app.generate_image = _image
    app.logger.info(
        "🧠 AI hotfix installed: smart replies temp=%.2f context=%s; image providers Pixazo/Gemini/HF/Pollinations/Horde",
        AI_RESPONSE_TEMPERATURE,
        SMART_CONTEXT_MAX_MESSAGES,
    )
