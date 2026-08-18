import asyncio
from collections import defaultdict
from types import SimpleNamespace

import optimist_ai_runtime as runtime
import optimist_hotfixes as hotfixes
import optimist_group_memory as group_memory


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
            {"user": "Лена", "text": "Мне нравится этот вариант", "reply_to_user": "", "ts": 1},
            {"user": "Алекс", "text": "Почему?", "reply_to_user": "Лена", "ts": 2},
            {"user": "Лена", "text": "Потому что он логичнее", "reply_to_user": "Алекс", "ts": 3},
            {"user": "Алекс", "text": "А если теперь?", "reply_to_user": "Лена", "ts": 4},
        ],
        clean_user_text_for_llm=lambda text: text,
        ask_llm=ask_llm,
        local_fallback=lambda name, mood: "fallback",
        GROQ_API_KEY=None,
        GROQ_MODEL="openai/gpt-oss-120b",
    )
    return app, captured


def test_base_hotfix_still_keeps_followup_context_for_private_fallback_path():
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
        assert "Контекстный режим: followup" in captured["system"]
        assert "Алекс → Лена" in captured["system"]
        assert "Лена → Алекс" in captured["system"]
        expected_temp = 0.68 if mood_key == "humor" else hotfixes.AI_RESPONSE_TEMPERATURE
        assert captured["temperature"] == expected_temp


def test_base_hotfix_greeting_reset_prevents_topic_leakage():
    app, captured = _fake_chat_app("optimist")
    app.get_recent_messages = lambda chat_id, limit=40: [
        {"user": "Алекс", "text": "Оптимист нарисуй Полину", "reply_to_user": "", "ts": 1},
        {"user": "Бот", "text": "Опиши Полину", "reply_to_user": "Алекс", "ts": 2},
        {"user": "Алекс", "text": "привет ты оптимист", "reply_to_user": "", "ts": 3},
    ]
    result = asyncio.run(hotfixes.smart_get_llm_response(app, "привет ты оптимист", -100, "alex"))
    assert result == "готовый ответ"
    assert "Контекстный режим: reset" in captured["system"]
    assert "Полин" not in captured["system"]
    assert "самостоятельная реплика" in captured["system"]


def test_private_classifier_and_group_legacy_contract_are_both_explicit():
    # Base/private classifier remains available for the old hotfix fallback path.
    assert hotfixes.context_mode("привет ты оптимист") == "reset"
    assert hotfixes.context_mode("Как ты?") == "reset"
    assert hotfixes.context_mode("О жив") == "reset"
    assert hotfixes.context_mode("Почему?") == "followup"
    assert hotfixes.context_mode("А если она дороже?") == "followup"
    assert hotfixes.context_mode("Что думаешь о новой функции?") == "normal"

    # Production group brain no longer classifies topics; it passes one broad transcript.
    for text in ["Как ты?", "О жив", "Что там?", "Стендап любишь", "Почему?"]:
        assert group_memory.group_context_mode(text) == "legacy"


def test_natural_draw_intents_accept_bot_addressing():
    assert hotfixes.normalize_draw_text("нарисуй Полину") == "нарисуй Полину"
    assert hotfixes.normalize_draw_text("Оптимист нарисуй Полину") == "нарисуй Полину"
    assert hotfixes.normalize_draw_text("Оптимист, нарисуй Полину") == "нарисуй Полину"
    assert hotfixes.normalize_draw_text("бот: сделай картинку кота") == "сделай картинку кота"
    assert hotfixes.normalize_draw_text("привет нарисуй Полину") is None


def test_fallback_is_transparent_not_motivational_template():
    assert "AI-провайдер" in hotfixes._fallback_response("Алекс", "Что думаешь?", "optimist")
    assert "Разберёмся и вытащим" not in hotfixes._fallback_response("Алекс", "Что думаешь?", "optimist")
    assert "Живой" in hotfixes._fallback_response("Алекс", "Как ты?", "optimist")


def test_legacy_brain_preserves_specialized_investor_reasoning_flow():
    app, _ = _fake_chat_app("investor_genius")
    calls = []

    async def specialized(user_text, chat_id, user_name):
        calls.append((user_text, chat_id, user_name))
        return "investor reasoning"

    previous = group_memory._BASE_GET_LLM
    try:
        group_memory._BASE_GET_LLM = specialized
        result = asyncio.run(group_memory.legacy_get_llm_response(app, hotfixes, "разбери проект", -100, "alex"))
    finally:
        group_memory._BASE_GET_LLM = previous

    assert result == "investor reasoning"
    assert calls == [("разбери проект", -100, "alex")]


def test_mode_contract_contains_all_five_modes():
    app, _ = _fake_chat_app("optimist")
    modes = runtime.mode_contract(app)
    assert set(modes) == {"optimist", "pessimist", "humor", "investor_genius", "mafioso"}
