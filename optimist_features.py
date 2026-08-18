#!/usr/bin/env python3
"""Extra production features for Optimist Bot.

Features are deliberately isolated from the original bot module:
- /awards, /awards_on, /awards_off
- /social
- /alert, /alerts, /delalert
- background weekly awards and crypto alert loops
"""

import asyncio
import datetime
import html
import os
import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Router, types
from aiogram.filters import Command
from aiogram.types import BotCommand

import optimist_bot_complete_final as app


feature_router = Router(name="optimist_extra_features")

AWARDS_TZ = os.getenv("AWARDS_TZ", app.MORNING_TZ)
AWARDS_WEEKDAY = int(os.getenv("AWARDS_WEEKDAY", "6"))  # Monday=0, Sunday=6
AWARDS_HOUR = int(os.getenv("AWARDS_HOUR", "20"))
ALERT_CHECK_SECONDS = max(60, int(os.getenv("ALERT_CHECK_SECONDS", "300")))
MAX_ALERTS_PER_USER = max(1, int(os.getenv("MAX_ALERTS_PER_USER", "8")))

FEATURE_COMMANDS = [
    BotCommand(command="awards", description="Награды группы за неделю"),
    BotCommand(command="awards_on", description="Включить еженедельные награды"),
    BotCommand(command="awards_off", description="Выключить еженедельные награды"),
    BotCommand(command="social", description="Кто с кем чаще общается"),
    BotCommand(command="alert", description="Алерт BTC/USDT по цене"),
    BotCommand(command="alerts", description="Мои активные алерты"),
    BotCommand(command="delalert", description="Удалить алерт по номеру"),
]


def _name(item: Dict[str, Any]) -> str:
    return (item.get("user") or item.get("username") or "Участник").strip()[:80]


def _recent(chat_id: int, hours: int = 168) -> List[Dict[str, Any]]:
    return app.get_recent_messages(chat_id, hours=hours, limit=app.SUMMARY_MAX_MESSAGES)


def _display_name(names: Dict[int, str], uid: Optional[int]) -> str:
    if not uid:
        return "—"
    return html.escape(names.get(int(uid), f"ID {uid}"))


def build_awards(chat_id: int, hours: int = 168) -> str:
    messages = _recent(chat_id, hours)
    if len(messages) < 5:
        return f"🏆 За последние {hours} ч. пока мало сообщений для нормальных наград."

    msg_count: Counter[int] = Counter()
    replies_received: Counter[int] = Counter()
    questions: Counter[int] = Counter()
    positive: Counter[int] = Counter()
    night: Counter[int] = Counter()
    names: Dict[int, str] = {}

    tz = ZoneInfo(AWARDS_TZ)
    for m in messages:
        uid = m.get("user_id")
        if not uid:
            continue
        uid = int(uid)
        names[uid] = _name(m)
        text = m.get("text") or ""
        msg_count[uid] += 1
        if "?" in text:
            questions[uid] += 1
        positive[uid] += max(0, app.message_sentiment_score(text))
        reply_to = m.get("reply_to_user_id")
        if reply_to:
            replies_received[int(reply_to)] += 1
        try:
            local_dt = datetime.datetime.fromtimestamp(float(m.get("ts", 0)), tz)
            if 0 <= local_dt.hour < 5:
                night[uid] += 1
        except Exception:
            pass

    def winner(counter: Counter[int]) -> Optional[int]:
        return counter.most_common(1)[0][0] if counter else None

    talker = winner(msg_count)
    magnet = winner(replies_received)
    curious = winner(questions)
    positive_uid = winner(positive)
    owl = winner(night)

    total = len(messages)
    lines = [
        "🏆 <b>OPTIMIST WEEKLY AWARDS</b>",
        f"<i>Период: последние {hours} ч. · сообщений: {total}</i>",
        "",
        f"🗣 <b>Главный двигатель чата:</b> {_display_name(names, talker)} — {msg_count.get(talker, 0)} сообщений",
        f"🧲 <b>Магнит ответов:</b> {_display_name(names, magnet)} — {replies_received.get(magnet, 0)} ответов ему/ей",
        f"❓ <b>Главный интервьюер:</b> {_display_name(names, curious)} — {questions.get(curious, 0)} сообщений с вопросами",
        f"✨ <b>Позитивный заряд:</b> {_display_name(names, positive_uid)} — больше всего позитивных маркеров",
        f"🌙 <b>Ночной житель:</b> {_display_name(names, owl)} — {night.get(owl, 0)} сообщений с 00:00 до 04:59",
        "",
        "🎬 <i>Цифры сказал Оптимист. Обиды принимаются строго вместе с мемами.</i>",
    ]
    return "\n".join(lines)


@feature_router.message(Command("awards"))
async def cmd_awards(message: types.Message):
    hours = app.parse_hours_arg(message.text or "", default=168)
    await message.reply(build_awards(message.chat.id, hours))


@feature_router.message(Command("awards_on"))
async def cmd_awards_on(message: types.Message):
    if not await app.is_admin(message.chat.id, message.from_user.id):
        await message.reply("⚠️ Включать еженедельные награды может только администратор.")
        return
    s = app.chat_settings[str(message.chat.id)]
    s["weekly_awards_enabled"] = True
    s.setdefault("last_weekly_awards_date", "")
    app.save_settings()
    await message.reply(
        f"🏆 Еженедельные награды включены: воскресенье около {AWARDS_HOUR:02d}:00 ({html.escape(AWARDS_TZ)})."
    )


@feature_router.message(Command("awards_off"))
async def cmd_awards_off(message: types.Message):
    if not await app.is_admin(message.chat.id, message.from_user.id):
        await message.reply("⚠️ Выключать еженедельные награды может только администратор.")
        return
    app.chat_settings[str(message.chat.id)]["weekly_awards_enabled"] = False
    app.save_settings()
    await message.reply("🏆 Автоматические еженедельные награды выключены.")


def build_social_graph(chat_id: int, hours: int = 168) -> str:
    messages = _recent(chat_id, hours)
    names: Dict[int, str] = {}
    directed: Counter[Tuple[int, int]] = Counter()
    undirected: Counter[Tuple[int, int]] = Counter()

    for m in messages:
        uid = m.get("user_id")
        if not uid:
            continue
        uid = int(uid)
        names[uid] = _name(m)
        target = m.get("reply_to_user_id")
        if not target:
            continue
        target = int(target)
        if uid == target:
            continue
        if m.get("reply_to_user"):
            names.setdefault(target, str(m.get("reply_to_user"))[:80])
        directed[(uid, target)] += 1
        undirected[tuple(sorted((uid, target)))] += 1

    if not undirected:
        return "🤝 Пока мало reply-связей. Отвечайте друг другу через Reply — и я построю карту общения."

    top_pairs = undirected.most_common(5)
    lines = [
        "🤝 <b>КАРТА ОБЩЕНИЯ</b>",
        f"<i>Последние {hours} ч. · считаю только реальные Reply-связи</i>",
        "",
    ]
    for idx, ((a, b), count) in enumerate(top_pairs, 1):
        ab = directed.get((a, b), 0)
        ba = directed.get((b, a), 0)
        lines.append(
            f"{idx}. <b>{_display_name(names, a)} ↔ {_display_name(names, b)}</b> — {count} reply · {ab}:{ba}"
        )

    most_balanced = None
    best_score = -1
    for (a, b), count in undirected.items():
        ab, ba = directed.get((a, b), 0), directed.get((b, a), 0)
        if ab and ba:
            score = min(ab, ba) * 100 - abs(ab - ba)
            if score > best_score:
                best_score = score
                most_balanced = (a, b, ab, ba)
    if most_balanced:
        a, b, ab, ba = most_balanced
        lines += [
            "",
            f"⚖️ <b>Самый взаимный диалог:</b> {_display_name(names, a)} ↔ {_display_name(names, b)} ({ab}:{ba})",
        ]
    return "\n".join(lines)


@feature_router.message(Command("social", "connections"))
async def cmd_social(message: types.Message):
    hours = app.parse_hours_arg(message.text or "", default=168)
    await message.reply(build_social_graph(message.chat.id, hours))


def _parse_alert(text: str) -> Optional[Tuple[str, str, float, str]]:
    # /alert btc > 95000 usd
    # /alert btc 95000 usd   -> >= target
    parts = (text or "").lower().replace(",", ".").split()
    if len(parts) < 3:
        return None
    symbol = parts[1].upper()
    if symbol not in {"BTC", "USDT"}:
        return None

    op = ">="
    value_idx = 2
    if parts[2] in {">", ">=", "<", "<="}:
        op = parts[2]
        value_idx = 3
    if value_idx >= len(parts):
        return None
    try:
        target = float(parts[value_idx])
    except ValueError:
        return None
    if target <= 0:
        return None

    currency = parts[value_idx + 1].upper() if value_idx + 1 < len(parts) else "USD"
    if currency not in {"USD", "RUB"}:
        return None
    return symbol, op, target, currency


def _alert_matches(price: float, op: str, target: float) -> bool:
    if op == ">":
        return price > target
    if op == ">=":
        return price >= target
    if op == "<":
        return price < target
    if op == "<=":
        return price <= target
    return False


def _alerts_for_chat(chat_id: int) -> List[Dict[str, Any]]:
    s = app.chat_settings[str(chat_id)]
    alerts = s.setdefault("crypto_alerts", [])
    if not isinstance(alerts, list):
        alerts = []
        s["crypto_alerts"] = alerts
    return alerts


def _fmt_price(value: float, currency: str) -> str:
    if currency == "RUB":
        return f"{value:,.2f} ₽".replace(",", " ")
    return f"${value:,.2f}".replace(",", " ")


@feature_router.message(Command("alert"))
async def cmd_alert(message: types.Message):
    parsed = _parse_alert(message.text or "")
    if not parsed:
        await message.reply(
            "🚨 Формат: <code>/alert btc &gt; 95000 usd</code>\n"
            "Или проще: <code>/alert btc 95000 usd</code> (сработает при цене ≥ цели).\n"
            "Поддерживаются BTC/USDT и USD/RUB."
        )
        return
    symbol, op, target, currency = parsed
    alerts = _alerts_for_chat(message.chat.id)
    mine = [a for a in alerts if int(a.get("user_id", 0)) == message.from_user.id]
    if len(mine) >= MAX_ALERTS_PER_USER:
        await message.reply(f"🚨 У тебя уже {len(mine)} алертов. Удали лишний через /alerts и /delalert N.")
        return

    alerts.append({
        "symbol": symbol,
        "op": op,
        "target": target,
        "currency": currency,
        "user_id": message.from_user.id,
        "user_name": message.from_user.first_name or "участник",
        "created_ts": time.time(),
    })
    app.save_settings()
    await message.reply(f"🚨 Алерт создан: <b>{symbol} {html.escape(op)} {_fmt_price(target, currency)}</b>")


@feature_router.message(Command("alerts"))
async def cmd_alerts(message: types.Message):
    alerts = _alerts_for_chat(message.chat.id)
    mine = [(idx, a) for idx, a in enumerate(alerts, 1) if int(a.get("user_id", 0)) == message.from_user.id]
    if not mine:
        await message.reply("🚨 У тебя нет активных crypto alerts.")
        return
    lines = ["🚨 <b>МОИ АЛЕРТЫ</b>", ""]
    for idx, a in mine:
        lines.append(
            f"#{idx} · {html.escape(str(a.get('symbol')))} {html.escape(str(a.get('op')))} "
            f"{_fmt_price(float(a.get('target', 0)), str(a.get('currency', 'USD')))}"
        )
    lines += ["", "Удалить: <code>/delalert N</code>"]
    await message.reply("\n".join(lines))


@feature_router.message(Command("delalert"))
async def cmd_delalert(message: types.Message):
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
        await message.reply("Используй <code>/delalert N</code>, где N — номер из /alerts.")
        return
    idx = int(parts[1].lstrip("#")) - 1
    alerts = _alerts_for_chat(message.chat.id)
    if idx < 0 or idx >= len(alerts):
        await message.reply("Такого алерта нет.")
        return
    alert = alerts[idx]
    if int(alert.get("user_id", 0)) != message.from_user.id and not await app.is_admin(message.chat.id, message.from_user.id):
        await message.reply("⚠️ Можно удалять только свои алерты.")
        return
    removed = alerts.pop(idx)
    app.save_settings()
    await message.reply(
        f"🗑 Удалён алерт {html.escape(str(removed.get('symbol')))} {html.escape(str(removed.get('op')))} "
        f"{_fmt_price(float(removed.get('target', 0)), str(removed.get('currency', 'USD')))}"
    )


async def _market_prices() -> Dict[Tuple[str, str], float]:
    prices: Dict[Tuple[str, str], float] = {}
    try:
        async with app.aiohttp.ClientSession() as session:
            data = await app.fetch_json(
                session,
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,tether&vs_currencies=usd,rub",
                timeout=12,
            )
        if data:
            for symbol, key in (("BTC", "bitcoin"), ("USDT", "tether")):
                for currency in ("USD", "RUB"):
                    value = data.get(key, {}).get(currency.lower())
                    if value is not None:
                        prices[(symbol, currency)] = float(value)
    except Exception as exc:
        app.logger.warning("Crypto price fetch error: %s", exc)
    return prices


async def crypto_alert_loop():
    while True:
        try:
            chats_with_alerts = [
                (cid, s.get("crypto_alerts", []))
                for cid, s in list(app.chat_settings.items())
                if isinstance(s.get("crypto_alerts", []), list) and s.get("crypto_alerts")
            ]
            if chats_with_alerts:
                prices = await _market_prices()
                changed = False
                for cid, alerts in chats_with_alerts:
                    remaining = []
                    for alert in list(alerts):
                        symbol = str(alert.get("symbol", "")).upper()
                        currency = str(alert.get("currency", "USD")).upper()
                        price = prices.get((symbol, currency))
                        if price is None or not _alert_matches(price, str(alert.get("op", ">=")), float(alert.get("target", 0))):
                            remaining.append(alert)
                            continue
                        user_name = html.escape(str(alert.get("user_name") or "Инвестор"))
                        target = float(alert.get("target", 0))
                        op = html.escape(str(alert.get("op", ">=")))
                        text = (
                            "🚨 <b>OPTIMIST MARKET ALERT</b>\n\n"
                            f"{user_name}, <b>{symbol}</b> достиг условия <b>{op} {_fmt_price(target, currency)}</b>.\n"
                            f"Текущая цена: <b>{_fmt_price(price, currency)}</b>.\n\n"
                            "📈 Сигнал выполнен и удалён. Без FOMO — сначала цифры, потом кнопки."
                        )
                        try:
                            await app.bot.send_message(int(cid), text)
                            changed = True
                        except Exception as exc:
                            app.logger.warning("Crypto alert send error chat=%s: %s", cid, exc)
                            remaining.append(alert)
                    if len(remaining) != len(alerts):
                        app.chat_settings[cid]["crypto_alerts"] = remaining
                        changed = True
                if changed:
                    app.save_settings()
        except Exception as exc:
            app.logger.error("Crypto alert loop error: %s", exc)
        await asyncio.sleep(ALERT_CHECK_SECONDS)


async def weekly_awards_loop():
    tz = ZoneInfo(AWARDS_TZ)
    while True:
        try:
            now = datetime.datetime.now(tz)
            if now.weekday() == AWARDS_WEEKDAY and now.hour == AWARDS_HOUR:
                today = now.date().isoformat()
                for cid, s in list(app.chat_settings.items()):
                    if not s.get("weekly_awards_enabled", False):
                        continue
                    if s.get("last_weekly_awards_date") == today:
                        continue
                    try:
                        await app.bot.send_message(int(cid), build_awards(int(cid), 168))
                        s["last_weekly_awards_date"] = today
                        app.save_settings()
                    except Exception as exc:
                        app.logger.warning("Weekly awards send error chat=%s: %s", cid, exc)
        except Exception as exc:
            app.logger.error("Weekly awards loop error: %s", exc)
        await asyncio.sleep(300)


def start_feature_tasks() -> None:
    asyncio.create_task(crypto_alert_loop())
    asyncio.create_task(weekly_awards_loop())
