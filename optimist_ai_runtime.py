#!/usr/bin/env python3
"""Current AI model routing and safe provider health checks for Optimist Bot."""

import os
import urllib.parse
from typing import Any, Dict

import aiohttp


CURRENT_GROQ_MODEL = os.getenv("OPTIMIST_GROQ_MODEL", "openai/gpt-oss-120b").strip() or "openai/gpt-oss-120b"
CURRENT_GROQ_FAST_MODEL = os.getenv("OPTIMIST_GROQ_FAST_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
CURRENT_OPENROUTER_REASONING_MODEL = os.getenv(
    "OPTIMIST_OPENROUTER_REASONING_MODEL", "deepseek/deepseek-r1-0528:free"
).strip() or "deepseek/deepseek-r1-0528:free"
CURRENT_GEMINI_IMAGE_MODEL = os.getenv(
    "OPTIMIST_GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"
).strip() or "gemini-3.1-flash-image"

DEPRECATED_GROQ_MODELS = {
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
}
LEGACY_OPENROUTER_REASONING_MODELS = {
    "deepseek/deepseek-r1:free",
}
LEGACY_GEMINI_IMAGE_MODELS = {
    "gemini-2.5-flash-image-preview",
    "gemini-2.5-flash-image",
}


def install(app) -> None:
    """Migrate known stale model IDs while keeping explicit escape hatches."""
    allow_legacy = os.getenv("OPTIMIST_ALLOW_LEGACY_MODELS", "0").strip().lower() in {"1", "true", "yes", "on"}

    old_primary = getattr(app, "GROQ_MODEL", "")
    old_fast = getattr(app, "GROQ_FAST_MODEL", "")
    if not allow_legacy and old_primary in DEPRECATED_GROQ_MODELS:
        app.GROQ_MODEL = CURRENT_GROQ_MODEL
    if not allow_legacy and old_fast in DEPRECATED_GROQ_MODELS:
        app.GROQ_FAST_MODEL = CURRENT_GROQ_FAST_MODEL

    old_reasoning = getattr(app, "OPENROUTER_REASONING_MODEL", "")
    if not allow_legacy and old_reasoning in LEGACY_OPENROUTER_REASONING_MODELS:
        app.OPENROUTER_REASONING_MODEL = CURRENT_OPENROUTER_REASONING_MODEL

    old_image = getattr(app, "GEMINI_IMAGE_MODEL", "")
    if not allow_legacy and old_image in LEGACY_GEMINI_IMAGE_MODELS:
        app.GEMINI_IMAGE_MODEL = CURRENT_GEMINI_IMAGE_MODEL

    app.logger.info(
        "🤖 Current AI routing: Groq=%s fast=%s | Gemini=%s image=%s | OpenRouter reasoning=%s | GitHub=%s",
        getattr(app, "GROQ_MODEL", "none"),
        getattr(app, "GROQ_FAST_MODEL", "none"),
        getattr(app, "GEMINI_MODEL", "none"),
        getattr(app, "GEMINI_IMAGE_MODEL", "none"),
        getattr(app, "OPENROUTER_REASONING_MODEL", "none"),
        getattr(app, "GITHUB_MODELS_MODEL", "none"),
    )


def effective_models(app) -> Dict[str, str]:
    return {
        "groq": str(getattr(app, "GROQ_MODEL", "none")),
        "groq_fast": str(getattr(app, "GROQ_FAST_MODEL", "none")),
        "gemini": str(getattr(app, "GEMINI_MODEL", "none")),
        "gemini_image": str(getattr(app, "GEMINI_IMAGE_MODEL", "none")),
        "openrouter": str(getattr(app, "OPENROUTER_MODEL", "none")),
        "openrouter_reasoning": str(getattr(app, "OPENROUTER_REASONING_MODEL", "none")),
        "github": str(getattr(app, "GITHUB_MODELS_MODEL", "none")),
        "github_reasoning": str(getattr(app, "GITHUB_REASONING_MODEL", "none")),
    }


def mode_contract(app) -> Dict[str, str]:
    """Expose configured personalities for tests/status without changing them."""
    result: Dict[str, str] = {}
    for key, value in getattr(app, "MOODS", {}).items():
        result[str(key)] = str(value.get("name") or key)
    return result


async def _get(session: aiohttp.ClientSession, url: str, headers: Dict[str, str]) -> tuple[int, str]:
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            text = await resp.text()
            return resp.status, text[:500]
    except Exception as exc:
        return 0, str(exc)[:240]


async def probe_apis(app) -> Dict[str, Dict[str, Any]]:
    """Lightweight provider checks. Never returns or logs secret values."""
    result: Dict[str, Dict[str, Any]] = {}
    async with aiohttp.ClientSession() as session:
        # Groq: authenticated model metadata endpoint; no completion tokens consumed.
        if getattr(app, "GROQ_API_KEY", None):
            model = urllib.parse.quote(str(app.GROQ_MODEL), safe="")
            status, body = await _get(
                session,
                f"https://api.groq.com/openai/v1/models/{model}",
                {"Authorization": f"Bearer {app.GROQ_API_KEY}"},
            )
            result["Groq"] = {"ok": status == 200, "status": status, "detail": str(app.GROQ_MODEL) if status == 200 else body[:120]}
        else:
            result["Groq"] = {"ok": False, "status": None, "detail": "key not configured"}

        # Gemini: list models using API-key header and verify both text + image IDs exist.
        if getattr(app, "GEMINI_API_KEY", None):
            status, body = await _get(
                session,
                "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
                {"x-goog-api-key": str(app.GEMINI_API_KEY)},
            )
            text_model = str(getattr(app, "GEMINI_MODEL", ""))
            image_model = str(getattr(app, "GEMINI_IMAGE_MODEL", ""))
            models_ok = status == 200 and text_model in body and image_model in body
            result["Gemini"] = {
                "ok": models_ok,
                "status": status,
                "detail": f"text={text_model}, image={image_model}" if models_ok else (body[:120] or "models unavailable"),
            }
        else:
            result["Gemini"] = {"ok": False, "status": None, "detail": "key not configured"}

        # OpenRouter: current-key metadata validates a normal API key without a generation request.
        if getattr(app, "OPENROUTER_API_KEY", None):
            status, body = await _get(
                session,
                "https://openrouter.ai/api/v1/key",
                {"Authorization": f"Bearer {app.OPENROUTER_API_KEY}"},
            )
            result["OpenRouter"] = {
                "ok": status == 200,
                "status": status,
                "detail": str(getattr(app, "OPENROUTER_REASONING_MODEL", "")) if status == 200 else body[:120],
            }
        else:
            result["OpenRouter"] = {"ok": False, "status": None, "detail": "key not configured"}

        # GitHub Models: authenticated catalog check; no inference tokens consumed.
        github_token = getattr(app, "GITHUB_MODELS_TOKEN", None)
        if github_token:
            status, body = await _get(
                session,
                "https://models.github.ai/catalog/models",
                {
                    "Authorization": f"Bearer {github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            )
            model = str(getattr(app, "GITHUB_MODELS_MODEL", ""))
            model_ok = status == 200 and model in body
            result["GitHub Models"] = {
                "ok": model_ok,
                "status": status,
                "detail": model if model_ok else (body[:120] or "model unavailable"),
            }
        else:
            result["GitHub Models"] = {"ok": False, "status": None, "detail": "token not configured"}

    return result
