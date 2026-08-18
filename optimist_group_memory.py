#!/usr/bin/env python3
"""Group-chat conversation memory and live Groq inference diagnostics.

This runtime layer restores the behavior Optimist had when it felt like a real
participant in a Telegram group: it sees recent messages from all participants,
remembers its own last replies, keeps the active topic for ambiguous short
phrases, and still lets explicit new topics take priority.
"""

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp


GROUP_CONTEXT_MAX_MESSAGES = max(20, int(os.getenv("GROUP_CONTEXT_MAX_MESSAGES", "42")))
GROUP_CONTEXT_MAX_CHARS = max(6000, int(os.getenv("GROUP_CONTEXT_MAX_CHARS", "18000")))
GROUP_CHAT_TEMPERATURE = min(1.0, max(0.0, float(os.getenv("GROUP_CHAT_TEMPERATURE", "0.64"))))
GROUP_GROQ_FALLBACK_MODEL = os.getenv("GROUP_GROQ_FALLBACK_MODEL", "qwen/qwen3.6-27b").strip()
ASSISTANT_MEMORY_MAX = max(20, int(os.getenv("ASSISTANT_MEMORY_MAX", "120")))

AI_DIAGNOSTICS: Dict[str, Any] = {
    "last_provider": "none",
    "last_model": "none",
    "last_status": None,
    "last_error": "",
    "last_latency_ms": 0,
    "last_ts": 0.0,
}

_BASE_SMART = None
_BASE_GET_LLM = None

_GREETING_RE = re.compile(
    r"^(?:привет|приветик|здравствуй|здравствуйте|здорово|хай|hello|hi|"
    r"привет\s+(?:ты\s+)?оптимист|ты\s+оптимист)[!?.\s]*$",
    re.I,
)

_FOLLOWUP_RE = re.compile(
    r"^(?:почему|зачем|а\s+если|и\s+что|а\s+что|тогда|а\s+как|и\s+как|"
    r"а\s+он|а\s+она|а\s+они|а\s+это|и\s+это|это|он|она|они|да|нет|точно|"
    r"серьезно|серьёзно|правда|дальше|продолжай|ну\s+и|и\?|а\?)\b",
    re.I,
)

_AMBIENT_RE = re.compile(
    r"^(?:как\s+дела|как\s+ты|ты\s+как|жив(?:ёшь|ешь)?|живой|о\s+жив|"
    r"что\s+там|ч[её]\s+там|ну\s+чего|ну\s+что|что\s+нового|ну\s+как|"
    r"как\s+оно|что\s+скажешь|чего\s+там)[!?.\s]*$",
    re.I,
)


def _clean_addressing(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^@\w+\s*[,!:;\-]?\s*", "", value, flags=re.I)
    value = re.sub(r"^(?:оптимист|бот)\s*[,!:;\-]?\s*", "", value, flags=re.I)
    return value.strip()


def group_context_mode(text: str, current_item: Optional[Dict[str, Any]] = None) -> str:
    """Classify a group message without throwing away useful ambient context."""
    cleaned = _clean_addressing(text).lower().strip()
    if _GREETING_RE.fullmatch(cleaned):
        return "reset"
    if current_item and current_item.get("reply_to_user_id"):
        return "followup"
    if _FOLLOWUP_RE.search(cleaned):
        return "followup"
    if _AMBIENT_RE.fullmatch(cleaned):
        return "ambient"
    words = re.findall(r"[а-яёa-z0-9]+", cleaned, flags=re.I)
    if len(words) <= 5 and any(token in cleaned for token in ["это", "он", "она", "они", "там", "тогда"]):
        return "followup"
    return "normal"


def _assistant_memory(app, chat_id: int) -> List[Dict[str, Any]]:
    raw = app.chat_settings[str(chat_id)].get("assistant_memory", [])
    return raw if isinstance(raw, list) else []


def _prior_group_history(app, chat_id: int, current_item: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Pull extra human messages because assistant turns are merged separately.
    humans = app.get_recent_messages(chat_id, limit=GROUP_CONTEXT_MAX_MESSAGES * 2)
    if humans:
        # main_handler stores the current user message immediately before asking AI.
        humans = humans[:-1]
    assistants = _assistant_memory(app, chat_id)
    combined = [*humans, *assistants]
    combined.sort(key=lambda item: float(item.get("ts", 0.0)))
    return combined[-GROUP_CONTEXT_MAX_MESSAGES:]


def _select_group_context(prior: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    if mode == "reset":
        return []
    if mode == "followup":
        return prior[-30:]
    if mode == "ambient":
        # Ambiguous phrases such as "что там?" or "живёшь?" should know what
        # the group has been discussing, like the original bot did.
        return prior[-26:]
    return prior[-18:]


def build_group_context(messages: List[Dict[str, Any]], max_chars: int = GROUP_CONTEXT_MAX_CHARS) -> str:
    lines: List[str] = []
    used = 0
    for item in reversed(messages):
        text = (item.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        is_bot = bool(item.get("is_bot"))
        author = "Optimist Bot" if is_bot else (item.get("user") or item.get("username") or "участник")
        reply_to = (item.get("reply_to_user") or "").strip()
        prefix = f"{author} → {reply_to}" if reply_to else str(author)
        line = f"{prefix}: {text[:1200]}"
        cost = len(line) + 1
        if lines and used + cost > max_chars:
            break
        lines.append(line)
        used += cost
    lines.reverse()
    return "\n".join(lines) if lines else "Контекста нет."


def _groq_payload(model: str, system_prompt: str, user_text: str, max_tokens: int, temperature: float) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "medium"
        payload["reasoning_format"] = "hidden"
    elif model.startswith("qwen/"):
        payload["reasoning_effort"] = "none"
        payload["reasoning_format"] = "hidden"
    return payload


def _extract_content(data: Dict[str, Any]) -> Optional[str]:
    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    except Exception:
        return None
    return None


def _set_ai_diag(provider: str, model: str, status: Optional[int], latency_ms: int, error: str = "") -> None:
    AI_DIAGNOSTICS.update({
        "last_provider": provider,
        "last_model": model,
        "last_status": status,
        "last_error": (error or "")[:240],
        "last_latency_ms": int(latency_ms),
        "last_ts": time.time(),
    })


def ai_diag_text() -> str:
    provider = AI_DIAGNOSTICS.get("last_provider") or "none"
    model = AI_DIAGNOSTICS.get("last_model") or "none"
    status = AI_DIAGNOSTICS.get("last_status")
    latency = AI_DIAGNOSTICS.get("last_latency_ms") or 0
    error = AI_DIAGNOSTICS.get("last_error") or ""
    base = f"{provider}/{model}"
    if status:
        base += f" HTTP {status}"
    if latency:
        base += f" {latency}ms"
    if error:
        base += f" — {error}"
    return base


async def groq_chat(app, model: str, system_prompt: str, user_text: str, max_tokens: int, temperature: float) -> Optional[str]:
    if not getattr(app, "GROQ_API_KEY", None) or not model:
        return None
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {app.GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=_groq_payload(model, system_prompt, user_text, max_tokens, temperature),
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                data = await resp.json(content_type=None)
                latency = int((time.monotonic() - started) * 1000)
                if resp.status != 200:
                    detail = str(data)[:220]
                    _set_ai_diag("Groq", model, resp.status, latency, detail)
                    app.logger.warning("Groq group chat %s -> %s: %s", model, resp.status, detail)
                    return None
                content = _extract_content(data)
                if content:
                    _set_ai_diag("Groq", model, resp.status, latency)
                    return content
                _set_ai_diag("Groq", model, resp.status, latency, "empty completion")
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        _set_ai_diag("Groq", model, None, latency, str(exc))
        app.logger.warning("Groq group chat %s error: %s", model, exc)
    return None


async def group_smart_get_llm_response(app, hotfixes, user_text: str, chat_id: int, user_name: str) -> str:
    global _BASE_SMART

    # Keep private chats and the specialized investment reasoning flow unchanged.
    mood_key = app.chat_settings[str(chat_id)].get("mood", "optimist")
    if chat_id >= 0 or mood_key == "investor_genius":
        return await _BASE_SMART(app, user_text, chat_id, user_name)

    settings = app.chat_settings[str(chat_id)]
    mood = app.MOODS.get(mood_key, app.MOODS["optimist"])
    length = app.RESPONSE_LENGTHS.get(
        settings.get("response_length", "medium"),
        app.RESPONSE_LENGTHS["medium"],
    )
    allow_prof = settings.get("allow_profanity", False)

    humans = app.get_recent_messages(chat_id, limit=GROUP_CONTEXT_MAX_MESSAGES * 2)
    current_item = humans[-1] if humans else None
    prior = _prior_group_history(app, chat_id, current_item)
    mode = group_context_mode(user_text, current_item)
    selected = _select_group_context(prior, mode)
    context = build_group_context(selected)
    cleaned = app.clean_user_text_for_llm(user_text)

    prof_rule = (
        "Мат можно использовать умеренно, если он естественен для текущего режима и чата."
        if allow_prof
        else "Мат, грубость и оскорбления запрещены."
    )
    mode_rule = {
        "reset": (
            "Это чистое приветствие или явный перезапуск общения. Ответь на него само по себе и не тащи старую тему."
        ),
        "followup": (
            "Это продолжение. Восстанови точный референт из последних реплик участников И из последнего ответа Optimist Bot."
        ),
        "ambient": (
            "Это короткая двусмысленная реплика вроде 'что там?', 'ну чего?', 'как дела?' или 'живёшь?'. "
            "Она может относиться к текущей теме группы. Сначала естественно ответь на саму реплику, затем, если в истории есть очевидная активная тема, продолжи именно её."
        ),
        "normal": (
            "Текущая реплика имеет приоритет. Используй историю группы только если она реально связана по смыслу; явную новую тему не подменяй старой."
        ),
    }[mode]

    system_prompt = (
        f"{mood['prompt']}\n"
        f"{length['rule']}\n"
        f"{prof_rule}\n"
        "Ты не сервисный ассистент, а умный живой участник Telegram-группы. Ты следишь за беседой целиком: кто что сказал, кому ответили, "
        "какая тема сейчас активна и что только что ответил ты сам.\n"
        f"Режим контекста: {mode}. {mode_rule}\n"
        "Не становись вялым и канцелярским. Не предлагай пошаговые планы без просьбы. Не добавляй мотивационную воду. "
        "Можно шутить и цепляться за детали чата, но факты не выдумывай.\n"
        "Если вопрос содержательный — рассуждай причинно и конкретно. Если это короткая живая реплика — отвечай живо, без лекции.\n"
        f"Начинай ответ с @{user_name}. Не повторяй запрос дословно.\n\n"
        f"Последний релевантный фрагмент группового разговора:\n{context}"
    )

    # Quality-first Groq path. 120B stays primary; Qwen 3.6 is a quality
    # fallback before the old generic provider chain and the smaller 20B model.
    primary = str(getattr(app, "GROQ_MODEL", "") or "")
    groq_models = [model for model in [primary, GROUP_GROQ_FALLBACK_MODEL] if model]
    seen = set()
    for model in groq_models:
        if model in seen:
            continue
        seen.add(model)
        answer = await groq_chat(app, model, system_prompt, cleaned, length["max_tokens"], GROUP_CHAT_TEMPERATURE)
        if answer:
            return answer

    # Existing Gemini/OpenRouter/GitHub fallbacks remain available. The old
    # ask_llm may retry Groq, which is intentional if the direct request failed transiently.
    answer = await app.ask_llm(
        system_prompt,
        cleaned,
        length["max_tokens"],
        temperature=GROUP_CHAT_TEMPERATURE,
    )
    if answer:
        _set_ai_diag("fallback-chain", "configured", 200, 0)
        return answer
    return hotfixes._fallback_response(user_name, user_text, mood_key)


async def _record_assistant_turn(app, chat_id: int, user_name: str, response: str) -> None:
    if not response:
        return
    cid = str(chat_id)
    memory = app.chat_settings[cid].get("assistant_memory")
    if not isinstance(memory, list):
        memory = []
        app.chat_settings[cid]["assistant_memory"] = memory
    memory.append({
        "text": response[:1600],
        "user": "Optimist Bot",
        "username": getattr(app, "BOT_USERNAME", None) or "optimist_bot",
        "user_id": getattr(app, "BOT_ID", None) or 0,
        "reply_to_user": user_name[:80],
        "reply_to_user_id": None,
        "is_bot": True,
        "ts": time.time(),
    })
    del memory[:-ASSISTANT_MEMORY_MAX]
    # Persist both the latest human messages and assistant memory without
    # blocking the event loop on JSON I/O.
    try:
        await asyncio.to_thread(app.save_settings)
    except Exception as exc:
        app.logger.warning("assistant memory save failed: %s", exc)


async def probe_groq_inference(app) -> Dict[str, Dict[str, Any]]:
    """Real completion probes: verifies that the Railway key can perform inference."""
    if not getattr(app, "GROQ_API_KEY", None):
        return {"Groq inference": {"ok": False, "status": None, "detail": "key not configured"}}

    models = []
    for model in [str(getattr(app, "GROQ_MODEL", "") or ""), GROUP_GROQ_FALLBACK_MODEL]:
        if model and model not in models:
            models.append(model)

    results: Dict[str, Dict[str, Any]] = {}
    for model in models:
        started = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {app.GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=_groq_payload(
                        model,
                        "Health check. Reply with exactly OK.",
                        "ping",
                        24,
                        0.0,
                    ),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json(content_type=None)
                    latency = int((time.monotonic() - started) * 1000)
                    content = _extract_content(data) or ""
                    ok = resp.status == 200 and bool(content)
                    detail = f"{model}; {latency}ms; reply={content[:40]}" if ok else str(data)[:180]
                    results[f"Groq inference {model}"] = {
                        "ok": ok,
                        "status": resp.status,
                        "detail": detail,
                    }
        except Exception as exc:
            results[f"Groq inference {model}"] = {
                "ok": False,
                "status": 0,
                "detail": str(exc)[:180],
            }
    return results


def install(app, hotfixes) -> None:
    """Install group-specific dialogue memory after the existing hotfix layer."""
    global _BASE_SMART, _BASE_GET_LLM
    if _BASE_SMART is None:
        _BASE_SMART = hotfixes.smart_get_llm_response
    if _BASE_GET_LLM is None:
        _BASE_GET_LLM = app.get_llm_response

    async def _group_smart(user_text: str, chat_id: int, user_name: str) -> str:
        response = await group_smart_get_llm_response(app, hotfixes, user_text, chat_id, user_name)
        if chat_id < 0:
            await _record_assistant_turn(app, chat_id, user_name, response)
        return response

    # hotfixes.install() created app.get_llm_response as a closure that resolves
    # hotfixes.smart_get_llm_response dynamically. Replace both lookup points so
    # tests and runtime behave identically.
    hotfixes.smart_get_llm_response = lambda runtime_app, user_text, chat_id, user_name: group_smart_get_llm_response(
        runtime_app, hotfixes, user_text, chat_id, user_name
    )
    app.get_llm_response = _group_smart
    app.logger.info(
        "🧠 Group memory installed: humans+bot replies, max=%s, ambient topic mode, Groq primary=%s quality fallback=%s",
        GROUP_CONTEXT_MAX_MESSAGES,
        getattr(app, "GROQ_MODEL", "none"),
        GROUP_GROQ_FALLBACK_MODEL or "none",
    )
