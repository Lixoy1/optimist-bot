#!/usr/bin/env python3
"""Legacy-style conversation brain for Optimist Bot.

The May version felt natural because it gave a capable chat model a broad,
plain transcript and very few meta-rules. This module restores that contract
while keeping the useful production upgrades added later:
- current Groq models;
- persistent memory of Optimist Bot's own replies;
- live provider diagnostics;
- Railway startup readiness against real Groq inference;
- safe external fallback chain.

There is deliberately no reset/followup/ambient state machine deciding what
history the model may see. The model receives one rolling Telegram transcript
and resolves the topic itself, like the original implementation did.
"""

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp


# The old code used CONTEXT_MAX_MESSAGES=25 human messages. This unified window
# is a little larger because it now also includes the bot's own replies.
GROUP_CONTEXT_MAX_MESSAGES = max(25, int(os.getenv("GROUP_CONTEXT_MAX_MESSAGES", "36")))
GROUP_CONTEXT_MAX_CHARS = max(8000, int(os.getenv("GROUP_CONTEXT_MAX_CHARS", "18000")))
ASSISTANT_MEMORY_MAX = max(30, int(os.getenv("ASSISTANT_MEMORY_MAX", "160")))

# Qwen 3.6 is the dialogue-oriented Groq replacement for retired Llama 3.3 70B.
# GPT-OSS 120B is the large quality/reasoning fallback.
LEGACY_DIALOGUE_MODEL = os.getenv("OPTIMIST_DIALOGUE_MODEL", "qwen/qwen3.6-27b").strip() or "qwen/qwen3.6-27b"
GROUP_GROQ_FALLBACK_MODEL = os.getenv("GROUP_GROQ_FALLBACK_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
REQUIRE_GROQ_READY = os.getenv("REQUIRE_GROQ_READY", "1").strip().lower() not in {"0", "false", "no", "off"}

# Qwen's current non-thinking guidance favors lower temperature for coherence;
# Humor gets a wider creative range while the normal persona stays logical.
MODE_TEMPERATURES = {
    "optimist": float(os.getenv("OPTIMIST_TEMP", "0.72")),
    "pessimist": float(os.getenv("PESSIMIST_TEMP", "0.76")),
    "humor": float(os.getenv("HUMOR_TEMP", "0.84")),
    "mafioso": float(os.getenv("MAFIOSO_TEMP", "0.78")),
}

AI_DIAGNOSTICS: Dict[str, Any] = {
    "last_provider": "none",
    "last_model": "none",
    "last_status": None,
    "last_error": "",
    "last_latency_ms": 0,
    "last_ts": 0.0,
    "startup_ready": False,
    "startup_detail": "not checked",
}

_BASE_GET_LLM = None
_BASE_STARTUP = None


def _assistant_memory(app, chat_id: int) -> List[Dict[str, Any]]:
    raw = app.chat_settings[str(chat_id)].get("assistant_memory", [])
    return raw if isinstance(raw, list) else []


def _rolling_history(app, chat_id: int) -> List[Dict[str, Any]]:
    """Merge human turns with persisted Optimist turns in chronological order."""
    humans = app.get_recent_messages(chat_id, limit=GROUP_CONTEXT_MAX_MESSAGES * 2)
    if humans:
        # The catch-all handler stores the current user message immediately
        # before calling get_llm_response, so the last human item is excluded
        # and sent separately as the current user message.
        humans = humans[:-1]
    combined = [*humans, *_assistant_memory(app, chat_id)]
    combined.sort(key=lambda item: float(item.get("ts", 0.0)))
    return combined[-GROUP_CONTEXT_MAX_MESSAGES:]


def build_group_context(messages: List[Dict[str, Any]], max_chars: int = GROUP_CONTEXT_MAX_CHARS) -> str:
    """Format a plain Telegram transcript, preserving participants and replies."""
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
    return "\n".join(lines) if lines else "Контекста пока нет."


def group_context_mode(text: str, current_item: Optional[Dict[str, Any]] = None) -> str:
    """Compatibility helper: the legacy brain intentionally has one mode."""
    return "legacy"


def _legacy_system_prompt(app, mood_key: str, user_name: str, context: str) -> str:
    """Keep the May prompt contract intentionally short and model-led."""
    mood = app.MOODS.get(mood_key, app.MOODS["optimist"])
    extra = ""
    if mood_key == "humor":
        extra = (
            "\nЦепляй шутку за конкретную деталь текущей переписки: наблюдение, подкол, callback, абсурд или самоиронию. "
            "Не объясняй шутку и не превращай ответ в совет, если совета не просили."
        )
    elif mood_key == "mafioso":
        extra = "\nПодхватывай игровую тему как живой участник чата, а не как справочник по Мафии."

    return (
        f"{mood['prompt']}{extra}\n"
        f"Ты общаешься в Telegram. Начинай ответ строго с @{user_name}, затем продолжай.\n"
        "НЕ повторяй фразу пользователя. Не пиши 'ты спросил', 'по поводу', 'как я понял'.\n"
        "Отвечай сразу по существу, учитывая контекст, но не пересказывай его дословно.\n"
        "Это реальная переписка нескольких людей, включая твои предыдущие ответы. Сам пойми по смыслу, что относится к текущей реплике.\n\n"
        f"Контекст последних сообщений:\n{context}"
    )


def _clamp_temperature(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _groq_payload(
    model: str,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
    temperature: float,
    *,
    reasoning: bool = False,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": _clamp_temperature(temperature),
        "max_completion_tokens": max_tokens,
    }
    if model.startswith("qwen/"):
        payload["reasoning_effort"] = "default" if reasoning else "none"
        payload["reasoning_format"] = "hidden"
        payload["top_p"] = 0.90 if reasoning else 0.80
    elif model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "medium" if reasoning else "low"
        payload["include_reasoning"] = False
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
        pass
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
    ready = "ready" if AI_DIAGNOSTICS.get("startup_ready") else "not-ready"
    base = f"{provider}/{model}"
    if status:
        base += f" HTTP {status}"
    if latency:
        base += f" {latency}ms"
    if error:
        base += f" — {error}"
    return f"{base}; startup={ready}"


async def groq_chat(
    app,
    model: str,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
    temperature: float,
    *,
    reasoning: bool = False,
) -> Optional[str]:
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
                json=_groq_payload(model, system_prompt, user_text, max_tokens, temperature, reasoning=reasoning),
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
                latency = int((time.monotonic() - started) * 1000)
                if resp.status != 200:
                    detail = str(data)[:220]
                    _set_ai_diag("Groq", model, resp.status, latency, detail)
                    app.logger.warning("Groq legacy brain %s -> %s: %s", model, resp.status, detail)
                    return None
                content = _extract_content(data)
                if content:
                    _set_ai_diag("Groq", model, resp.status, latency)
                    return content
                _set_ai_diag("Groq", model, resp.status, latency, "empty completion")
    except Exception as exc:
        latency = int((time.monotonic() - started) * 1000)
        _set_ai_diag("Groq", model, None, latency, str(exc))
        app.logger.warning("Groq legacy brain %s error: %s", model, exc)
    return None


async def legacy_get_llm_response(app, hotfixes, user_text: str, chat_id: int, user_name: str) -> str:
    """Restore the original broad-context, low-micromanagement reply contract."""
    cid = str(chat_id)
    settings = app.chat_settings[cid]
    mood_key = settings.get("mood", "optimist")

    # The old investor mode already contains a purpose-built reasoning flow.
    if mood_key == "investor_genius" and _BASE_GET_LLM is not None:
        return await _BASE_GET_LLM(user_text, chat_id, user_name)

    length = app.RESPONSE_LENGTHS.get(
        settings.get("response_length", "medium"),
        app.RESPONSE_LENGTHS["medium"],
    )
    allow_prof = settings.get("allow_profanity", False)
    prof_rule = (
        "Мат можно использовать умеренно, если он уместен."
        if allow_prof
        else "Мат, грубость и оскорбления запрещены."
    )

    context = build_group_context(_rolling_history(app, chat_id))
    cleaned = app.clean_user_text_for_llm(user_text)
    system_prompt = (
        f"{_legacy_system_prompt(app, mood_key, user_name, context)}\n"
        f"{length['rule']}\n"
        f"{prof_rule}"
    )
    temperature = _clamp_temperature(MODE_TEMPERATURES.get(mood_key, 0.72))

    # Dialogue first. If the preview dialogue model is unavailable for the key,
    # fall through to the large production GPT-OSS model.
    for model in [LEGACY_DIALOGUE_MODEL, GROUP_GROQ_FALLBACK_MODEL]:
        answer = await groq_chat(
            app,
            model,
            system_prompt,
            cleaned,
            length["max_tokens"],
            temperature,
            reasoning=False,
        )
        if answer:
            return answer

    # Preserve Gemini/OpenRouter/GitHub Models and the old Groq chain as a final
    # resilience layer if both direct dialogue models fail transiently.
    answer = await app.ask_llm(
        system_prompt,
        cleaned,
        length["max_tokens"],
        temperature=temperature,
    )
    if answer:
        _set_ai_diag("fallback-chain", "configured", 200, 0)
        return answer
    return hotfixes._fallback_response(user_name, user_text, mood_key)


# Backward-compatible public name used by existing callers/tests.
group_smart_get_llm_response = legacy_get_llm_response


async def _record_assistant_turn(app, chat_id: int, user_name: str, response: str) -> None:
    if not response:
        return
    cid = str(chat_id)
    memory = app.chat_settings[cid].get("assistant_memory")
    if not isinstance(memory, list):
        memory = []
        app.chat_settings[cid]["assistant_memory"] = memory
    memory.append({
        "text": response[:1800],
        "user": "Optimist Bot",
        "username": getattr(app, "BOT_USERNAME", None) or "optimist_bot",
        "user_id": getattr(app, "BOT_ID", None) or 0,
        "reply_to_user": user_name[:80],
        "reply_to_user_id": None,
        "is_bot": True,
        "ts": time.time(),
    })
    del memory[:-ASSISTANT_MEMORY_MAX]
    try:
        await asyncio.to_thread(app.save_settings)
    except Exception as exc:
        app.logger.warning("assistant memory save failed: %s", exc)


async def probe_groq_inference(app) -> Dict[str, Dict[str, Any]]:
    """Real completion probes using the same models as the production brain."""
    if not getattr(app, "GROQ_API_KEY", None):
        return {"Groq inference": {"ok": False, "status": None, "detail": "key not configured"}}

    models: List[str] = []
    for model in [LEGACY_DIALOGUE_MODEL, GROUP_GROQ_FALLBACK_MODEL]:
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
                        "Reply with exactly OK.",
                        "ping",
                        24,
                        0.0,
                        reasoning=False,
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


async def verify_startup_readiness(app) -> Dict[str, Dict[str, Any]]:
    """Railway readiness gate: prove at least one real Groq completion works."""
    if not getattr(app, "GROQ_API_KEY", None):
        AI_DIAGNOSTICS["startup_ready"] = False
        AI_DIAGNOSTICS["startup_detail"] = "GROQ_API_KEY missing"
        if REQUIRE_GROQ_READY:
            raise RuntimeError("Groq readiness failed: GROQ_API_KEY is not configured")
        return {"Groq inference": {"ok": False, "status": None, "detail": "key not configured"}}

    probes = await probe_groq_inference(app)
    working = [name for name, info in probes.items() if info.get("ok")]
    AI_DIAGNOSTICS["startup_ready"] = bool(working)
    AI_DIAGNOSTICS["startup_detail"] = ", ".join(working) if working else "no working Groq inference model"
    if REQUIRE_GROQ_READY and not working:
        compact = "; ".join(f"{name}: {info.get('status')} {info.get('detail', '')[:100]}" for name, info in probes.items())
        raise RuntimeError(f"Groq readiness failed: {compact}")
    app.logger.info("✅ Groq startup readiness: %s", AI_DIAGNOSTICS["startup_detail"])
    return probes


def install(app, hotfixes) -> None:
    """Install one legacy-style brain and a real Groq startup readiness gate."""
    global _BASE_GET_LLM, _BASE_STARTUP
    if _BASE_GET_LLM is None:
        _BASE_GET_LLM = app.get_llm_response
    if _BASE_STARTUP is None:
        _BASE_STARTUP = app.on_startup

    async def _legacy_brain(user_text: str, chat_id: int, user_name: str) -> str:
        response = await legacy_get_llm_response(app, hotfixes, user_text, chat_id, user_name)
        await _record_assistant_turn(app, chat_id, user_name, response)
        return response

    async def _startup_with_ai_readiness():
        await _BASE_STARTUP()
        await verify_startup_readiness(app)

    app.get_llm_response = _legacy_brain
    app.on_startup = _startup_with_ai_readiness
    app.logger.info(
        "🧠 Legacy brain installed: transcript=%s turns, dialogue=%s, fallback=%s, humor-temp=%.2f, Groq-ready=%s",
        GROUP_CONTEXT_MAX_MESSAGES,
        LEGACY_DIALOGUE_MODEL,
        GROUP_GROQ_FALLBACK_MODEL,
        MODE_TEMPERATURES["humor"],
        REQUIRE_GROQ_READY,
    )
