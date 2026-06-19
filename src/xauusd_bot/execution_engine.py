"""execution-engine service — consumes ``decisions``, emits ``orders`` + ``journal``.

For every *qualified* decision it runs the entry gauntlet (risk → stops
→ TP → size → order) via :class:`ExecutionPipeline` and publishes an
:class:`OrderEvent` on ``orders`` plus :class:`JournalEvent` records on
``journal``.

Entry point for ``SERVICE_ROLE=execution-engine``.

Scope: this drives trade *entry*. Managing open positions over
subsequent bars (trailing stops, partial TP, emergency flatten) is the
not-yet-driven position-management loop — see
:mod:`xauusd_bot.execution.pipeline` and AGENTS.md.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    DecisionEvent,
    JournalEvent,
    OrderEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.runtime_config import (
    STATE_KEY_ACCOUNT,
    STATE_KEY_POSITIONS,
    STATE_KEY_RISK,
    get_emergency_stop,
    get_json,
    set_json,
)
from xauusd_bot.common.notify import TelegramNotifier
from xauusd_bot.common.schemas.journal import ExitReasonTag, TradeCloseUpdate
from xauusd_bot.common.service import make_consumer, make_publisher, service_runtime
from xauusd_bot.connectors.factory import make_connector
from xauusd_bot.connectors.schemas import OrderSide
from xauusd_bot.execution.pipeline import ExecutionPipeline
from xauusd_bot.execution.position_manager import ManagedPosition, PositionManager

# Redis key prefix for per-position management plans (TP/SL/trailing state).
_MGMT_KEY_PREFIX = "mgmt:pos:"

# Never open a NEW entry on a decision older than this (real wall-clock). Guards
# against a consumer backlog / any stale data replaying through the engine — a
# live decision is produced within a second of now.
_MAX_DECISION_AGE_SECONDS = 120.0

# Persisted RiskManager state (running daily/weekly realized PnL + day/week
# anchors) so the loss caps survive an execution-engine restart within the day.
_RISK_TRACKER_KEY = "state:risk_tracker"

log = structlog.get_logger(__name__)


async def _persist_risk(redis_client, risk_mgr) -> None:
    """Best-effort save of the running risk state to Redis."""
    with contextlib.suppress(Exception):
        await set_json(redis_client, _RISK_TRACKER_KEY, risk_mgr.state.model_dump(mode="json"))


async def _load_risk(redis_client, risk_mgr) -> None:
    """Restore the running risk state from Redis on startup (best-effort)."""
    with contextlib.suppress(Exception):
        data = await get_json(redis_client, _RISK_TRACKER_KEY)
        if data:
            risk_mgr.restore_state(data)
            log.info("execution_risk_state_restored", daily=data.get("daily_pnl"), weekly=data.get("weekly_pnl"))

GROUP = "execution-engine-v1"

# How often the operational state (account/positions/risk) is snapshotted
# to Redis for the dashboard's live cockpit.
_STATE_INTERVAL_SECONDS = 3.0


def _account_snapshot(acc) -> dict:
    def f(v):
        return float(v) if v is not None else None

    return {
        "balance": f(acc.balance),
        "equity": f(acc.equity),
        "margin": f(acc.margin),
        "free_margin": f(acc.free_margin),
        "currency": acc.currency,
        "leverage": acc.leverage,
        "daily_pnl": f(acc.daily_pnl),
        "weekly_pnl": f(acc.weekly_pnl),
        "current_spread": f(acc.current_spread),
        "trade_allowed": acc.trade_allowed,
        "server_time": acc.server_time.isoformat() if acc.server_time else None,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


def _risk_snapshot(acc, settings: Settings, n_positions: int, risk_mgr, unrealized_pnl: float = 0.0) -> dict:
    balance = float(acc.balance) if acc.balance is not None else 0.0
    # Realized running PnL from the RiskManager (the same numbers the loss caps
    # gate on) — NOT acc.daily_pnl, which the broker never populates.
    st = risk_mgr.state
    daily_pnl = float(st.daily_pnl)
    weekly_pnl = float(st.weekly_pnl)
    spread = float(acc.current_spread) if acc.current_spread is not None else None
    return {
        "daily_pnl": daily_pnl,
        "daily_loss_cap": -balance * settings.risk_max_daily,
        "daily_cap_pct": settings.risk_max_daily,
        "weekly_pnl": weekly_pnl,
        "weekly_loss_cap": -balance * settings.risk_max_weekly,
        "weekly_cap_pct": settings.risk_max_weekly,
        "trades_today": st.trades_today,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": n_positions,
        "max_open_positions": settings.risk_max_open_positions,
        "max_trades_per_session": settings.risk_max_trades_per_session,
        "spread_max_pips": settings.spread_max_pips,
        "current_spread_pips": (spread / 10.0) if spread is not None else None,
        "ts": datetime.now(tz=UTC).isoformat(),
    }


async def _sync_emergency(pipeline: ExecutionPipeline, redis_client, notifier=None) -> None:
    """Mirror the dashboard kill-switch onto the EmergencyStopManager.

    Engaging flattens + cancels the book and halts new entries (the
    RiskManager refuses while the stop is active); clearing releases it.
    """

    engaged = await get_emergency_stop(redis_client)
    active = pipeline.emergency.is_active()
    if engaged and not active:
        await asyncio.to_thread(pipeline.emergency.manual_trigger, "dashboard")
        log.warning("execution_emergency_engaged_from_dashboard")
        if notifier is not None and notifier.enabled:
            await notifier.send("⛔ <b>EMERGENCY STOP engaged</b> — trading halted, book flattened.")
    elif not engaged and active:
        await asyncio.to_thread(pipeline.emergency.clear)
        log.info("execution_emergency_cleared_from_dashboard")
        if notifier is not None and notifier.enabled:
            await notifier.send("✅ <b>Emergency stop cleared</b> — trading re-enabled.")


async def _publish_state(
    pipeline: ExecutionPipeline,
    connector,
    settings: Settings,
    redis_client,
    stop_event: asyncio.Event,
    notifier=None,
) -> None:
    """Snapshot account / positions / risk to Redis on a timer for the dashboard.

    Also syncs the operator kill-switch each tick (≤3s latency).
    """

    while not stop_event.is_set():
        try:
            await _sync_emergency(pipeline, redis_client, notifier)
        except Exception as exc:  # noqa: BLE001 - never let the kill-switch sync crash the loop
            log.warning("execution_emergency_sync_failed", error=str(exc))
        try:
            # Connector calls are sync (RPyC can block) — run off the loop.
            acc = await asyncio.to_thread(connector.get_account)
            positions = await asyncio.to_thread(connector.positions_get, settings.symbol)
            pos_snap = [
                {
                    "id": p.position_id,
                    "symbol": p.symbol,
                    "side": p.side.value,
                    "volume": float(p.volume),
                    "open_price": float(p.open_price),
                    "sl": float(p.sl) if p.sl is not None else None,
                    "tp": float(p.tp) if p.tp is not None else None,
                    "profit": float(p.profit),
                    "open_time": p.open_time.isoformat() if p.open_time else None,
                }
                for p in positions
            ]
            # Roll daily/weekly at the UTC boundary even with no trades, then
            # persist so a restart keeps the running totals. Unrealized = sum of
            # open-position broker profit (shown alongside the realized caps).
            pipeline.risk_mgr.roll(datetime.now(tz=UTC))
            await _persist_risk(redis_client, pipeline.risk_mgr)
            unrealized = float(sum(p.profit for p in positions)) if positions else 0.0
            await set_json(redis_client, STATE_KEY_ACCOUNT, _account_snapshot(acc))
            await set_json(redis_client, STATE_KEY_POSITIONS, pos_snap)
            await set_json(redis_client, STATE_KEY_RISK, _risk_snapshot(acc, settings, len(positions), pipeline.risk_mgr, unrealized))
        except Exception as exc:  # noqa: BLE001 - never let state publishing kill the service
            log.warning("execution_state_publish_failed", error=str(exc))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=_STATE_INTERVAL_SECONDS)


async def _store_managed(redis_client, mp: ManagedPosition) -> None:
    # Long TTL — the plan must outlive the position (deleted explicitly when the
    # position closes), unlike the 15s state snapshots set_json defaults to.
    await set_json(redis_client, _MGMT_KEY_PREFIX + mp.ticket, mp.model_dump(mode="json"), ttl=2_592_000)


async def _delete_managed(redis_client, ticket: str) -> None:
    with contextlib.suppress(Exception):
        await redis_client.delete(_MGMT_KEY_PREFIX + ticket)


async def _load_managed_all(redis_client) -> dict[str, ManagedPosition]:
    out: dict[str, ManagedPosition] = {}
    async for key in redis_client.scan_iter(match=_MGMT_KEY_PREFIX + "*"):
        data = await get_json(redis_client, key)
        if not data:
            continue
        try:
            mp = ManagedPosition.model_validate(data)
            out[mp.ticket] = mp
        except Exception as exc:  # noqa: BLE001 - drop a corrupt plan, keep going
            log.warning("execution_managed_plan_invalid", key=key, error=str(exc))
    return out


async def _apply_action(connector, ticket: str, action) -> None:
    if action.kind == "modify_sl":
        await asyncio.to_thread(connector.order_modify, ticket, sl=float(action.price))
    elif action.kind in ("partial_close", "close_all"):
        fn = getattr(connector, "close_position", None)
        if fn is None:
            log.warning("execution_close_not_supported", ticket=ticket)
            return
        await asyncio.to_thread(fn, ticket, action.volume if action.kind == "partial_close" else None)


def _r_multiple(side: OrderSide, entry: Decimal, sl: Decimal, exit_price: Decimal) -> float | None:
    """R = reward / initial risk. None when risk is undefined (entry == sl)."""
    risk = abs(entry - sl)
    if risk == 0:
        return None
    reward = (exit_price - entry) if side == OrderSide.BUY else (entry - exit_price)
    return float(reward / risk)


# MT5 DEAL_REASON_* → our exit tag. 4 = SL, 5 = TP (the only two the broker tags).
_MT5_REASON_SL = 4
_MT5_REASON_TP = 5


def _exit_reason(mp: ManagedPosition, exit_price: Decimal, reason_code: int | None) -> ExitReasonTag:
    """Best-effort close reason: broker deal reason first, else price proximity."""
    if reason_code == _MT5_REASON_SL:
        return ExitReasonTag.TRAILED if mp.breakeven_done else ExitReasonTag.SL_HIT
    if reason_code == _MT5_REASON_TP:
        if mp.tp2_taken:
            return ExitReasonTag.TP3_HIT
        return ExitReasonTag.TP2_HIT if mp.tp1_taken else ExitReasonTag.TP1_HIT
    # No tagged reason → infer from the closest configured level.
    levels = [(mp.sl_price, ExitReasonTag.TRAILED if mp.breakeven_done else ExitReasonTag.SL_HIT)]
    for px, tag in ((mp.tp1_price, ExitReasonTag.TP1_HIT), (mp.tp2_price, ExitReasonTag.TP2_HIT), (mp.tp3_price, ExitReasonTag.TP3_HIT)):
        if px is not None:
            levels.append((px, tag))
    nearest = min(levels, key=lambda lt: abs(exit_price - lt[0]))
    # Only attribute to a level if we're genuinely close to it; else manual.
    if abs(exit_price - nearest[0]) <= abs(mp.entry_price - mp.sl_price):
        return nearest[1]
    return ExitReasonTag.MANUAL


async def _journal_close(publisher, connector, mp: ManagedPosition, ticket, current_price, settings, risk_mgr=None, redis_client=None) -> None:
    """Finalise a closed position in the journal (best-effort — never raises).

    Pulls the broker deal history for exit price + realized PnL when the
    connector supports it; otherwise falls back to the last bar price (PnL
    left unknown). Publishes a ``trade_close`` journal event; the journal-writer
    resolves the ticket to the open trade and applies the close fields.
    """
    try:
        info = None
        fn = getattr(connector, "closed_position_info", None)
        if fn is not None:
            info = await asyncio.to_thread(fn, ticket)
        if info is not None:
            exit_price = Decimal(str(info.exit_price))
            pnl = Decimal(str(info.pnl_realized))
            close_time = info.close_time
            reason_code = info.reason_code
        else:
            # No deal history → approximate exit at the last seen price.
            exit_price = Decimal(str(current_price))
            pnl = None
            close_time = datetime.now(tz=UTC)
            reason_code = None
        update = TradeCloseUpdate(
            order_id=str(ticket),
            timestamp_close=close_time,
            exit_price=exit_price,
            pnl_realized=pnl,
            r_multiple=_r_multiple(mp.side, mp.entry_price, mp.sl_price, exit_price),
            exit_reason=_exit_reason(mp, exit_price, reason_code),
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=settings.symbol, entry_type="trade_close", trade_close=update),
        )
        log.info("execution_trade_close_journaled", ticket=ticket, exit=str(exit_price), pnl=(str(pnl) if pnl is not None else None))
        # Book the realized PnL into the running risk totals so the daily/weekly
        # loss caps actually accumulate (and persist it). Use real wall-clock for
        # the day/week rollover. Skip when the broker gave no PnL (no deal history).
        if risk_mgr is not None and pnl is not None:
            risk_mgr.record_pnl(pnl, datetime.now(tz=UTC))
            if redis_client is not None:
                await _persist_risk(redis_client, risk_mgr)
    except Exception as exc:  # noqa: BLE001 - journaling must never disturb management
        log.warning("execution_trade_close_journal_failed", ticket=ticket, error=str(exc))


async def _manage_positions(pipeline, pos_mgr, redis_client, settings, bundle, current_price, notifier=None, publisher=None) -> None:
    """Drive each tracked open position forward one bar (TP partials / SL trail)."""
    stored = await _load_managed_all(redis_client)
    if not stored:
        return
    connector = pipeline.connector
    positions = await asyncio.to_thread(connector.positions_get, settings.symbol)
    open_tickets = {p.position_id for p in (positions or [])}
    price = Decimal(str(current_price))
    for ticket, mp in stored.items():
        if ticket not in open_tickets:
            # Position closed on the broker → finalise the journal trade, then
            # drop the management plan.
            if publisher is not None:
                await _journal_close(publisher, connector, mp, ticket, current_price, settings, pipeline.risk_mgr, redis_client)
            await _delete_managed(redis_client, ticket)
            continue
        actions, mp2 = pos_mgr.plan(mp, bundle, price)
        for a in actions:
            try:
                await _apply_action(connector, ticket, a)
                log.info(
                    "execution_manage_action",
                    ticket=ticket,
                    kind=a.kind,
                    reason=a.reason,
                    price=(str(a.price) if a.price is not None else None),
                    volume=(str(a.volume) if a.volume is not None else None),
                )
                if notifier is not None and notifier.enabled:
                    icon = {"partial_close": "📊", "modify_sl": "🛡", "close_all": "🏁"}.get(a.kind, "•")
                    detail = (f"{a.volume} lots" if a.volume is not None else (str(a.price) if a.price is not None else ""))
                    await notifier.send(f"{icon} <b>MANAGE</b> {a.kind} ({a.reason}) · {settings.symbol} #{ticket} {detail}")
            except Exception as exc:  # noqa: BLE001 - one bad action must not stall the loop
                log.warning("execution_manage_apply_failed", ticket=ticket, kind=a.kind, error=str(exc))
        if actions:
            await _store_managed(redis_client, mp2)


def _make_handler(pipeline: ExecutionPipeline, publisher: Publisher, redis_client, settings: Settings, notifier=None):
    pos_mgr = PositionManager(pipeline.stop_mgr, pipeline.tp_mgr, pipeline.spec)

    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, DecisionEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("execution_engine_dropping_unknown_version", version=ev.schema_version)
            return

        # 1. Manage open positions EVERY bar (trailing / TP partials), even on
        #    no_trade — the bundle + ref_price ride on every decision event.
        if ev.bundle is not None and ev.ref_price is not None:
            try:
                await _manage_positions(pipeline, pos_mgr, redis_client, settings, ev.bundle, ev.ref_price, notifier, publisher)
            except Exception as exc:  # noqa: BLE001 - management must never kill the service
                log.warning("execution_manage_failed", error=str(exc))

        # 2. Entry — only for a qualified setup.
        qual = ev.qualification
        if qual is None or not qual.qualified:
            return
        if ev.ref_price is None:
            log.warning("execution_engine_no_ref_price", setup=qual.qualification_id)
            return

        # SAFETY (defense in depth, independent of the RiskManager):
        # (a) hard-stop the kill-switch at entry time, and
        # (b) never enter on a stale decision (backlog / replay / backfill) —
        #     a live decision is produced within ~1s of now.
        if await get_emergency_stop(redis_client):
            log.warning("execution_entry_skipped_emergency_stop", setup=qual.qualification_id)
            return
        age = (datetime.now(tz=UTC) - (ev.produced_at or datetime.now(tz=UTC))).total_seconds()
        if age > _MAX_DECISION_AGE_SECONDS:
            log.warning("execution_entry_skipped_stale_decision", age_seconds=round(age), setup=qual.qualification_id)
            return

        now = ev.decision.timestamp or ev.produced_at or datetime.now(tz=UTC)
        outcome = pipeline.process(
            ev.decision, ev.score, qual, ev.bundle, ref_price=ev.ref_price, now=now
        )
        if not outcome.submitted:
            log.info("execution_engine_blocked", reason=outcome.blocked_reason)
            if notifier is not None and notifier.enabled and (outcome.blocked_reason or "").startswith("order_rejected"):
                await notifier.send(f"🔴 <b>ORDER REJECTED</b> · {ev.symbol} · {outcome.blocked_reason}")
            return
        if outcome.managed is not None:
            await _store_managed(redis_client, outcome.managed)
        if notifier is not None and notifier.enabled and outcome.order is not None:
            o = outcome.order
            await notifier.send(
                f"🟢 <b>ENTRY</b> {str(getattr(o, 'side', '')).upper()} {getattr(o, 'volume', '')} {ev.symbol}"
                f" @ {getattr(o, 'fill_price', None) or '?'} · score {round(ev.score.total_score)}"
            )

        # Idempotency: the consumer is at-least-once. The order's
        # client_order_id (set by OrderManager) is the dedupe key the
        # broker/journal use to reject a replayed submission.
        await publisher.publish(
            StreamTopic.ORDERS, OrderEvent(symbol=ev.symbol, order=outcome.order)
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=ev.symbol, entry_type="trade", trade=outcome.trade),
        )
        await publisher.publish(
            StreamTopic.JOURNAL,
            JournalEvent(symbol=ev.symbol, entry_type="order", order=outcome.order),
        )

    return handle


async def _run(settings: Settings) -> int:
    import redis.asyncio as aioredis

    connector = make_connector(settings)
    pipeline = ExecutionPipeline(settings, connector)
    publisher = make_publisher(settings)
    await publisher.connect()
    state_redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    # Restore the running risk totals so the daily/weekly loss caps survive a
    # restart within the same day/week.
    await _load_risk(state_redis, pipeline.risk_mgr)
    consumer = make_consumer(
        settings,
        StreamTopic.DECISIONS,
        GROUP,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )
    notifier = TelegramNotifier.from_settings(settings)
    log.info("execution_engine_alerts", telegram_enabled=notifier.enabled)
    handler = _make_handler(pipeline, publisher, state_redis, settings, notifier)

    # service_runtime gives us the shared stop_event so the consumer loop
    # and the state-publisher task shut down together.
    async with service_runtime(ServiceRole.EXECUTION_ENGINE) as stop:
        state_task = asyncio.create_task(
            _publish_state(pipeline, connector, settings, state_redis, stop, notifier),
            name="execution-state-publisher",
        )
        log.info("execution_engine_consuming", topic=StreamTopic.DECISIONS.value, group=GROUP)
        try:
            await consumer.run_forever(handler, DecisionEvent, stop_event=stop)
        finally:
            state_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state_task
            await consumer.close()
            await publisher.close()
            await state_redis.aclose()
            with contextlib.suppress(Exception):
                connector.shutdown()
    return 0


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
