import asyncio
from collections import defaultdict
from types import SimpleNamespace

import optimist_ai_runtime as runtime
import optimist_hotfixes as hotfixes


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def test_stale_provider_models_are_migrated():
    app = SimpleNamespace(
        GROQ_MODEL="llama-3.3-70b-versatile",
        GROQ_FAST_MODEL="llama-3.1-8b-instant",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_IMAGE_MODEL="gemini-2.5-flash-image",
        OPENROUTER_REASONING_MODEL="deepseek/deepseek-r1:free",
        GITHUB_MODELS_MODEL="openai/gpt-4o-mini",
        logger=_Logger(),
    )
    runtime.install(app)
    assert app.GROQ_MODEL == "openai/gpt-oss-120b"
    assert app.GROQ_FAST_MODEL == "openai/gpt-oss-20b"
    assert app.OPENROUTER_REASONING_MODEL == "deepseek/deepseek-r1-0528:free"
    assert app.GEMINI_IMAGE_MODEL == "gemini-3.1-flash-image"


def test_current_model_summary_is_explicit():
    app = SimpleNamespace(
        GROQ_MODEL="openai/gpt-oss-120b",
        GROQ_FAST_MODEL="openai/gpt-oss-20b",
        GEMINI_MODEL="gemini-2.5-flash",
        GEMINI_IMAGE_MODEL="gemini-3.1-flash-image",
        OPENROUTER_MODEL="deepseek/deepseek-r1:free",
        OPENROUTER_REASONING_MODEL="deepseek/deepseek-r1-0528:free",
        GITHUB_MODELS_MODEL="openai/gpt-4o-mini",
        GITHUB_REASONING_MODEL="deepseek/DeepSeek-R1-0528",
    )
    models = runtime.effective_models(app)
    assert models["groq"] == "openai/gpt-oss-120b"
    assert models["gemini_image"] == "gemini-3.1-flash-image"
    assert models["openrouter_reasoning"].endswith("r1-0528:free")


def _fake_chat_app(mood_key):
    captured = {}

    async def ask_llm(system_prompt, user_text, max_tokens, temperature=0.8, reasoning=False):
        captured["system"] = system_prompt
        captured["user"] = user_text
        captured["temperature"] = temperature
        return "готовый ответ"

    moods = {
        "optimist": {"name": "Оптимист", "prompt": "OPTIMIST-PROMPT"},
        "pessimist": {"name": "Пессимист", "prompt": "PESSIMIST-PROMPT"},
        "humor": {"name": "Юморист", "prompt": "HUMOR-PROMPT"},
        "investor_genius": {"name": "Гений инвестиций", "prompt": "INVESTOR-PROMPT"},
        "mafioso": {"name": "Мафиози", "prompt": "MAFIOSO-PROMPT"},
    }
    settings = defaultdict(dict)
    settings["-100"] = {
        "mood": mood_key,
        "response_length": "medium",
        "allow_profanity": False,
    }
    app = SimpleNamespace(
        chat_settings=settings,
        MOODS=moods,
        RESPONSE_LENGTHS={"medium": {"rule": "MEDIUM-RULE", "max_tokens": 500}},
        get_recent_messages=lambda chat_id, limit=40: [
            {"user": "Лена", "text": "Мне нравится этот вариант", "reply_to_user": ""},
            {"user": "Алекс", "text": "Почему?", "reply_to_user": "Лена"},
            {"user": "Лена", "text": "Потому что он логичнее", "reply_to_user": "Алекс"},
        ],
        clean_user_text_for_llm=lambda text: text,
        ask_llm=ask_llm,
        local_fallback=lambda name, mood: "fallback",
    )
    return app, captured


def test_all_normal_personality_modes_keep_smart_context():
    expected = {
        "optimist": "OPTIMIST-PROMPT",
        "pessimist": "PESSIMIST-PROMPT",
        "humor": "HUMOR-PROMPT",
        "mafioso": "MAFIOSO-PROMPT",
    }
    for mood_key, marker in expected.items():
        app, captured = _fake_chat_app(mood_key)
        result = asyncio.run(hotfixes.smart_get_llm_response(app, "А если теперь?", -100, "alex"))
        assert result == "готовый ответ"
        assert marker in captured["system"]
        assert "не отвечаешь на каждое сообщение изолированно" in captured["system"]
        assert "Алекс → Лена" in captured["system"]
        assert "Лена → Алекс" in captured["system"]
        if mood_key == "humor":
            assert captured["temperature"] == 0.70
        else:
            assert captured["temperature"] == hotfixes.AI_RESPONSE_TEMPERATURE


def test_investor_mode_preserves_specialized_reasoning_flow():
    app, _ = _fake_chat_app("investor_genius")
    calls = []

    async def original(user_text, chat_id, user_name):
        calls.append((user_text, chat_id, user_name))
        return "investor reasoning"

    previous = hotfixes._ORIGINAL_GET_LLM_RESPONSE
    try:
        hotfixes._ORIGINAL_GET_LLM_RESPONSE = original
        result = asyncio.run(hotfixes.smart_get_llm_response(app, "разбери проект", -100, "alex"))
    finally:
        hotfixes._ORIGINAL_GET_LLM_RESPONSE = previous

    assert result == "investor reasoning"
    assert calls == [("разбери проект", -100, "alex")]


def test_mode_contract_contains_all_five_modes():
    app, _ = _fake_chat_app("optimist")
    modes = runtime.mode_contract(app)
    assert set(modes) == {"optimist", "pessimist", "humor", "investor_genius", "mafioso"}
