import asyncio
from collections import defaultdict
from types import SimpleNamespace

import optimist_group_memory as gm


def test_old_style_ambiguous_phrases_keep_group_topic():
    assert gm.group_context_mode("Как дела") == "ambient"
    assert gm.group_context_mode("Живешь") == "ambient"
    assert gm.group_context_mode("Что там?") == "ambient"
    assert gm.group_context_mode("Ну чего") == "ambient"
    assert gm.group_context_mode("Стендап любишь") == "normal"
    assert gm.group_context_mode("Привет") == "reset"
    assert gm.group_context_mode("Почему?") == "followup"


def test_reply_is_always_followup():
    current = {"reply_to_user_id": 123}
    assert gm.group_context_mode("И что думаешь", current) == "followup"


def test_group_context_contains_people_and_bot_reply():
    text = gm.build_group_context([
        {"user": "Лена", "text": "Этот проект обещает 3 процента в день", "ts": 1},
        {"user": "Алекс", "text": "Похоже на хайп", "reply_to_user": "Лена", "ts": 2},
        {"user": "Optimist Bot", "text": "Да, тут есть красные флаги", "is_bot": True, "reply_to_user": "Алекс", "ts": 3},
    ])
    assert "Лена: Этот проект" in text
    assert "Алекс → Лена" in text
    assert "Optimist Bot → Алекс" in text
    assert "красные флаги" in text


def test_gpt_oss_request_uses_supported_medium_reasoning_contract():
    payload = gm._groq_payload("openai/gpt-oss-120b", "system", "user", 500, 0.64)
    assert payload["reasoning_effort"] == "medium"
    assert payload["include_reasoning"] is False
    assert "reasoning_format" not in payload
    assert payload["max_completion_tokens"] == 500


def test_qwen_quality_fallback_uses_dialogue_mode():
    payload = gm._groq_payload("qwen/qwen3.6-27b", "system", "user", 500, 0.64)
    assert payload["reasoning_effort"] == "none"
    assert payload["reasoning_format"] == "hidden"
    assert "include_reasoning" not in payload


def test_group_prompt_recreates_ambient_topic_behavior(monkeypatch):
    captured = {}

    async def fake_groq(app, model, system_prompt, user_text, max_tokens, temperature):
        captured["model"] = model
        captured["system"] = system_prompt
        captured["user"] = user_text
        captured["temperature"] = temperature
        return "@Алекс жив, бро. По тому TikTok-проекту всё ещё много красных флагов."

    monkeypatch.setattr(gm, "groq_chat", fake_groq)

    settings = defaultdict(dict)
    settings["-100"] = {
        "mood": "optimist",
        "response_length": "medium",
        "allow_profanity": False,
        "assistant_memory": [
            {
                "text": "По TikTok-проекту доходность выглядит подозрительно.",
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

    async def fallback_llm(*args, **kwargs):
        raise AssertionError("fallback should not be reached when Groq works")

    app = SimpleNamespace(
        chat_settings=settings,
        MOODS={"optimist": {"prompt": "OPTIMIST"}},
        RESPONSE_LENGTHS={"medium": {"rule": "MEDIUM", "max_tokens": 500}},
        get_recent_messages=lambda chat_id, limit=100: humans[-limit:],
        clean_user_text_for_llm=lambda text: text,
        ask_llm=fallback_llm,
        GROQ_MODEL="openai/gpt-oss-120b",
    )
    hotfixes = SimpleNamespace(_fallback_response=lambda *args: "fallback")

    result = asyncio.run(gm.group_smart_get_llm_response(app, hotfixes, "Живешь", -100, "Алекс"))
    assert "TikTok" in result
    assert captured["model"] == "openai/gpt-oss-120b"
    assert "Режим контекста: ambient" in captured["system"]
    assert "Лена: Там обещают 3 процента" in captured["system"]
    assert "Optimist Bot → Алекс" in captured["system"]


def test_assistant_reply_is_saved_separately_from_human_stats():
    settings = defaultdict(dict)
    calls = []
    app = SimpleNamespace(
        chat_settings=settings,
        BOT_USERNAME="optimist_bot",
        BOT_ID=999,
        save_settings=lambda: calls.append("saved"),
        logger=SimpleNamespace(warning=lambda *args, **kwargs: None),
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
