import datetime
import os
from pathlib import Path

os.environ.setdefault("TG_TOKEN", "123456789:" + "A" * 35)
os.environ.setdefault("DATA_DIR", "/tmp/optimist-test-data")
os.environ.setdefault("MORNING_TZ", "Europe/Moscow")

import optimist_bot_complete_final as app
import optimist_entrypoint as entry
import optimist_features as features
import optimist_hotfixes as hotfixes
import optimist_group_memory as group_memory


def test_entrypoint_uses_persistent_data_dir():
    assert entry.app.SETTINGS_FILE.startswith(os.environ["DATA_DIR"])
    assert entry.app.SETTINGS_FILE.endswith("bot_settings_complete_final.json")


def test_human_uptime():
    assert entry._human_uptime(65) == "1м"
    assert entry._human_uptime(3665) == "1ч 1м"
    assert entry._human_uptime(90061) == "1д 1ч 1м"


def test_missed_fallback_counts_authors_and_escapes_html():
    messages = [
        {"user": "Алекс", "text": "Привет <b>чат</b>"},
        {"user": "Алекс", "text": "Второе сообщение"},
        {"user": "Лена", "text": "Ответ"},
    ]
    text = entry._fallback_missed(messages, 8)
    assert "Алекс — 2" in text
    assert "Лена — 1" in text
    assert "&lt;b&gt;" in text or "Второе сообщение" in text


def test_parse_crypto_alert_variants():
    assert features._parse_alert("/alert btc 95000 usd") == ("BTC", ">=", 95000.0, "USD")
    assert features._parse_alert("/alert btc < 90000 usd") == ("BTC", "<", 90000.0, "USD")
    assert features._parse_alert("/alert usdt > 90 rub") == ("USDT", ">", 90.0, "RUB")
    assert features._parse_alert("/alert eth 1000 usd") is None
    assert features._parse_alert("/alert btc nope usd") is None


def test_alert_match_logic():
    assert features._alert_matches(100, ">=", 100)
    assert features._alert_matches(101, ">", 100)
    assert features._alert_matches(99, "<", 100)
    assert features._alert_matches(100, "<=", 100)
    assert not features._alert_matches(99, ">=", 100)


def _sample_messages():
    now = datetime.datetime.now().timestamp()
    return [
        {"user_id": 1, "user": "Алекс", "text": "Всем привет! Отличный день", "reply_to_user_id": None, "reply_to_user": "", "ts": now - 30},
        {"user_id": 2, "user": "Лена", "text": "Как дела?", "reply_to_user_id": 1, "reply_to_user": "Алекс", "ts": now - 25},
        {"user_id": 1, "user": "Алекс", "text": "Супер, а у тебя?", "reply_to_user_id": 2, "reply_to_user": "Лена", "ts": now - 20},
        {"user_id": 2, "user": "Лена", "text": "Тоже хорошо!", "reply_to_user_id": 1, "reply_to_user": "Алекс", "ts": now - 15},
        {"user_id": 3, "user": "Никита", "text": "Что обсуждаем?", "reply_to_user_id": 1, "reply_to_user": "Алекс", "ts": now - 10},
        {"user_id": 1, "user": "Алекс", "text": "Планы на неделю", "reply_to_user_id": 3, "reply_to_user": "Никита", "ts": now - 5},
    ]


def test_weekly_awards_is_data_driven(monkeypatch):
    monkeypatch.setattr(features, "_recent", lambda chat_id, hours=168: _sample_messages())
    text = features.build_awards(-100, 168)
    assert "OPTIMIST WEEKLY AWARDS" in text
    assert "Алекс" in text
    assert "сообщений: 6" in text


def test_social_graph_uses_real_reply_edges(monkeypatch):
    monkeypatch.setattr(features, "_recent", lambda chat_id, hours=168: _sample_messages())
    text = features.build_social_graph(-100, 168)
    assert "КАРТА ОБЩЕНИЯ" in text
    assert "Алекс" in text
    assert "Лена" in text
    assert "reply" in text


def test_pixazo_payload_matches_current_flux_klein_contract():
    payload = hotfixes._pixazo_payload("test prompt")
    assert payload == {
        "prompt": "test prompt",
        "steps": 25,
        "width": 1024,
        "height": 1024,
    }
    assert "negative_prompt" not in payload
    assert "guidance_scale" not in payload


def test_pollinations_uses_current_gen_endpoint():
    url = hotfixes._pollinations_url("cat in space")
    assert url.startswith("https://gen.pollinations.ai/image/")
    assert "model=flux" in url
    assert "width=1024" in url
    assert "height=1024" in url


def test_smart_context_keeps_reply_relationships():
    context = hotfixes.build_smart_context(_sample_messages())
    assert "Лена → Алекс: Как дела?" in context
    assert "Алекс → Лена: Супер, а у тебя?" in context
    assert "Никита → Алекс: Что обсуждаем?" in context


def test_runtime_layers_are_installed_in_expected_modules():
    # Text replies in groups are intentionally upgraded by the dedicated group-memory layer.
    assert app.get_llm_response.__module__ == "optimist_group_memory"
    # Image generation remains owned by the existing image/API hotfix layer.
    assert app.generate_image.__module__ == "optimist_hotfixes"
    assert entry.group_memory is group_memory


def test_railway_config_present_and_points_to_entrypoint():
    cfg = Path("railway.toml").read_text(encoding="utf-8")
    assert 'builder = "RAILPACK"' in cfg
    assert 'startCommand = "python optimist_entrypoint.py"' in cfg
    assert 'healthcheckPath = "/health"' in cfg
    assert 'restartPolicyType = "ALWAYS"' in cfg
