import asyncio
from collections import defaultdict
from types import SimpleNamespace

import pytest

import optimist_group_memory as gm


class _Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


def _fake_app(mood="optimist"):
    settings = defaultdict(dict)
    settings["-100"] = {
        "mood": mood,
        "response_length": "medium",
        "allow_profanity": False,
        "assistant_memory": [
            {
                "text": "@Алекс По TikTok-проекту тут много красных флагов.",
                "user": "Optimist Bot",
                "is_bot": True,
                "reply_to_user": "Алекс",
                "ts": 3,
            }
        ],
    }
    humans = [
        {"user": "Лена", "text": "Там обещают 3 процента в день", "ts": 1},
        {"user": "Алекс", "text": "Что думаешь про TikTok проект?", "ts": 2},
        {"user": "Алекс", "text": "Живешь", "ts": 4},
    ]
    moods = {
        "optimist": {"prompt": "Ты — жизнерадостный оптимист. Поддерживаешь, вдохновляешь, отвечаешь живо. Не повторяй вопрос пользователя."},
        "pessimist": {"prompt": "Ты — саркастичный пессимист с чёрным юмором. Предупреждаешь о рисках, но не токсично. Не повторяй вопрос пользователя."},
        "humor": {"prompt": "Ты — стендап-комик. Отвечаешь шутками и мемами, но по делу. Не повторяй запрос."},
        "mafioso": {"prompt": "Ты — старый дон в стиле классической игры в Мафию."},
    }

    async def generic_fallback(*args, **kwargs):
        return "generic fallback"

    return SimpleNamespace(
        chat_settings=settings,
        MOODS=moods,
        RESPONSE_LENGTHS={"medium": {"rule": "Ответь одним компактным абзацем: 4-7 предложений.", "max_tokens": 550}},
        get_recent_messages=lambda chat_id, limit=100: humans[-limit:],
        clean_user_text_for_llm=lambda text: text.replace("Оптимист", "").strip(),
        ask_llm=generic_fallback,
        save_settings=lambda: None,
        BOT_USERNAME="optimist_bot",
        BOT_ID=999,
        GROQ_API_KEY="test-key",
        logger=_Logger(),
    )


def test_legacy_brain_uses_one_broad_context_mode():
    for text in ["Как дела", "Живешь", "Что там?", "Ну чего", "Стендап любишь", "Привет", "Почему?"]:
        assert gm.group_context_mode(text) == "legacy"


def test_may_prompt_contract_is_preserved_without_classifier_bureaucracy():
    app = _fake_app("optimist")
    prompt = gm._legacy_system_prompt(app, "optimist", "Алекс", "Лена: привет")
    assert app.MOODS["optimist"]["prompt"] in prompt
    assert "Ты общаешься в Telegram. Начинай ответ строго с @Алекс" in prompt
    assert "НЕ повторяй фразу пользователя" in prompt
    assert "Отвечай сразу по существу, учитывая контекст" in prompt
    assert "Контекст последних сообщений" in prompt
    assert "Режим контекста" not in prompt
    assert "reset" not in prompt
    assert "ambient" not in prompt
    assert "followup" not in prompt


def test_all_personality_temperatures_keep_logic_and_humor_headroom():
    assert 0.65 <= gm.MODE_TEMPERATURES["optimist"] <= 0.80
    assert gm.MODE_TEMPERATURES["pessimist"] >= gm.MODE_TEMPERATURES["optimist"]
    assert gm.MODE_TEMPERATURES["mafioso"] >= gm.MODE_TEMPERATURES["optimist"]
    assert gm.MODE_TEMPERATURES["humor"] > gm.MODE_TEMPERATURES["optimist"]
    assert gm.MODE_TEMPERATURES["humor"] <= 0.90


def test_context_contains_people_reply_edges_and_bot_turns():
    text = gm.build_group_context([
        {"user": "Лена", "text": "Этот проект обещает 3 процента в день", "ts": 1},
        {"user": "Алекс", "text": "Похоже на хайп", "reply_to_user": "Лена", "ts": 2},
        {"user": "Optimist Bot", "text": "Да, тут есть красные флаги", "is_bot": True, "reply_to_user": "Алекс", "ts": 3},
    ])
    assert "Лена: Этот проект" in text
    assert "Алекс → Лена" in text
    assert "Optimist Bot → Алекс" in text
    assert "красные флаги" in text


def test_qwen_dialogue_payload_is_non_thinking_and_hidden():
    payload = gm._groq_payload("qwen/qwen3.6-27b", "system", "user", 550, 0.72)
    assert payload["reasoning_effort"] == "none"
    assert payload["reasoning_format"] == "hidden"
    assert payload["top_p"] == 0.80
    assert payload["max_completion_tokens"] == 550


def test_gpt_oss_fallback_uses_supported_low_reasoning_contract():
    payload = gm._groq_payload("openai/gpt-oss-120b", "system", "user", 550, 0.72)
    assert payload["reasoning_effort"] == "low"
    assert payload["include_reasoning"] is False
    assert "reasoning_format" not in payload


def test_reasoning_payload_can_be_raised_for_analytical_work():
    payload = gm._groq_payload("openai/gpt-oss-120b", "system", "user", 550, 0.55, reasoning=True)
    assert payload["reasoning_effort"] == "medium"
    assert payload["include_reasoning"] is False


def test_old_style_short_phrase_gets_full_group_transcript(monkeypatch):
    app = _fake_app("optimist")
    captured = []

    async def fake_groq(app_obj, model, system_prompt, user_text, max_tokens, temperature, *, reasoning=False):
        captured.append((model, system_prompt, user_text, temperature, reasoning))
        return "@Алекс Ага, жив 😄 По TikTok-теме я всё ещё смотрю на эти 3% в день с прищуром."

    monkeypatch.setattr(gm, "groq_chat", fake_groq)
    hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")

    result = asyncio.run(gm.legacy_get_llm_response(app, hotfixes, "Живешь", -100, "Алекс"))
    assert "TikTok" in result
    assert captured[0][0] == "qwen/qwen3.6-27b"
    system = captured[0][1]
    assert "Контекст последних сообщений" in system
    assert "Лена: Там обещают 3 процента в день" in system
    assert "Optimist Bot → Алекс" in system
    assert "Режим контекста" not in system


def test_clear_new_topic_keeps_history_available_but_current_message_is_separate(monkeypatch):
    app = _fake_app("optimist")
    captured = {}

    async def fake_groq(app_obj, model, system_prompt, user_text, max_tokens, temperature, *, reasoning=False):
        captured["system"] = system_prompt
        captured["user"] = user_text
        return "@Алекс Да, стендап люблю. Особенно когда панч приходит быстрее, чем уведомление о выводе."

    monkeypatch.setattr(gm, "groq_chat", fake_groq)
    hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")

    result = asyncio.run(gm.legacy_get_llm_response(app, hotfixes, "Стендап любишь", -100, "Алекс"))
    assert "стендап" in result.lower()
    assert captured["user"] == "Стендап любишь"
    # The model still sees prior chat and decides semantic relevance itself.
    assert "TikTok" in captured["system"]


def test_humor_mode_is_hotter_and_has_contextual_standup_contract(monkeypatch):
    app = _fake_app("humor")
    captured = {}

    async def fake_groq(app_obj, model, system_prompt, user_text, max_tokens, temperature, *, reasoning=False):
        captured["model"] = model
        captured["system"] = system_prompt
        captured["temperature"] = temperature
        return "@Алекс Конечно. Мой стендап уже пережил два API и одну подписку Railway — публика пока держится."

    monkeypatch.setattr(gm, "groq_chat", fake_groq)
    hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")

    result = asyncio.run(gm.legacy_get_llm_response(app, hotfixes, "Анекдот", -100, "Алекс"))
    assert result.startswith("@Алекс")
    assert captured["model"] == gm.LEGACY_DIALOGUE_MODEL
    assert captured["temperature"] == gm.MODE_TEMPERATURES["humor"]
    assert captured["temperature"] > gm.MODE_TEMPERATURES["optimist"]
    assert "подкол" in captured["system"]
    assert "Не объясняй шутку" in captured["system"]


def test_each_non_investor_mode_uses_legacy_dialogue_model_first(monkeypatch):
    for mood in ["optimist", "pessimist", "humor", "mafioso"]:
        app = _fake_app(mood)
        calls = []

        async def fake_groq(app_obj, model, system_prompt, user_text, max_tokens, temperature, *, reasoning=False):
            calls.append((model, temperature, system_prompt))
            return f"@Алекс {mood} ответ"

        monkeypatch.setattr(gm, "groq_chat", fake_groq)
        hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")
        result = asyncio.run(gm.legacy_get_llm_response(app, hotfixes, "Проверка", -100, "Алекс"))
        assert result.startswith("@Алекс")
        assert calls[0][0] == gm.LEGACY_DIALOGUE_MODEL
        assert calls[0][1] == gm.MODE_TEMPERATURES[mood]
        assert app.MOODS[mood]["prompt"] in calls[0][2]


def test_primary_dialogue_model_falls_back_to_gpt_oss(monkeypatch):
    app = _fake_app("optimist")
    calls = []

    async def fake_groq(app_obj, model, system_prompt, user_text, max_tokens, temperature, *, reasoning=False):
        calls.append(model)
        if model == gm.LEGACY_DIALOGUE_MODEL:
            return None
        return "@Алекс Второй Groq сработал."

    monkeypatch.setattr(gm, "groq_chat", fake_groq)
    hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")
    result = asyncio.run(gm.legacy_get_llm_response(app, hotfixes, "Что там?", -100, "Алекс"))
    assert result.endswith("сработал.")
    assert calls == [gm.LEGACY_DIALOGUE_MODEL, gm.GROUP_GROQ_FALLBACK_MODEL]


def test_assistant_reply_is_persisted_for_future_context():
    settings = defaultdict(dict)
    calls = []
    app = SimpleNamespace(
        chat_settings=settings,
        BOT_USERNAME="optimist_bot",
        BOT_ID=999,
        save_settings=lambda: calls.append("saved"),
        logger=_Logger(),
    )
    asyncio.run(gm._record_assistant_turn(app, -100, "Алекс", "@Алекс Вот мой ответ"))
    memory = settings["-100"]["assistant_memory"]
    assert len(memory) == 1
    assert memory[0]["is_bot"] is True
    assert memory[0]["reply_to_user"] == "Алекс"
    assert calls == ["saved"]


def test_groq_probe_is_transparent_without_key():
    app = SimpleNamespace(GROQ_API_KEY=None)
    result = asyncio.run(gm.probe_groq_inference(app))
    assert result["Groq inference"]["ok"] is False
    assert result["Groq inference"]["status"] is None


def test_startup_readiness_accepts_one_real_working_model(monkeypatch):
    app = SimpleNamespace(GROQ_API_KEY="test-key", logger=_Logger())

    async def fake_probe(_app):
        return {
            "Groq inference qwen/qwen3.6-27b": {"ok": True, "status": 200, "detail": "OK"},
            "Groq inference openai/gpt-oss-120b": {"ok": False, "status": 429, "detail": "rate limited"},
        }

    monkeypatch.setattr(gm, "probe_groq_inference", fake_probe)
    result = asyncio.run(gm.verify_startup_readiness(app))
    assert result["Groq inference qwen/qwen3.6-27b"]["ok"] is True
    assert gm.AI_DIAGNOSTICS["startup_ready"] is True


def test_startup_readiness_fails_when_required_and_key_missing(monkeypatch):
    app = SimpleNamespace(GROQ_API_KEY=None, logger=_Logger())
    monkeypatch.setattr(gm, "REQUIRE_GROQ_READY", True)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        asyncio.run(gm.verify_startup_readiness(app))


def test_startup_readiness_fails_when_all_models_fail(monkeypatch):
    app = SimpleNamespace(GROQ_API_KEY="test-key", logger=_Logger())

    async def fake_probe(_app):
        return {
            "Groq inference qwen/qwen3.6-27b": {"ok": False, "status": 401, "detail": "bad key"},
            "Groq inference openai/gpt-oss-120b": {"ok": False, "status": 401, "detail": "bad key"},
        }

    monkeypatch.setattr(gm, "probe_groq_inference", fake_probe)
    monkeypatch.setattr(gm, "REQUIRE_GROQ_READY", True)
    with pytest.raises(RuntimeError, match="Groq readiness failed"):
        asyncio.run(gm.verify_startup_readiness(app))
