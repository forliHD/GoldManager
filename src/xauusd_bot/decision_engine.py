"""decision-engine service — consumes ``features``, emits ``decisions``.

Runs aggregate → score → (rule or AI) → qualify for every feature
bundle and publishes the :class:`Decision` + :class:`Score`
(+ :class:`TradeQualification`) plus the originating bundle (the
execution-engine needs it for SL/TP) on the ``decisions`` stream.

Entry point for ``SERVICE_ROLE=decision-engine``.

The decision layer is connector-free: per-bar account state is not
streamed, and :class:`RuleBasedFallback` treats a missing account as
"no block". The execution-engine performs the authoritative,
account-aware risk checks with its own connector.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from xauusd_bot.common.config import ServiceRole, Settings, load_settings
from xauusd_bot.common.logging import setup_logging
from xauusd_bot.common.messaging.compact import compact_bundle
from datetime import UTC, datetime

from xauusd_bot.common.messaging.events import (
    ENVELOPE_SCHEMA_VERSION,
    DecisionEvent,
    FeaturesEvent,
    JournalEvent,
)
from xauusd_bot.common.messaging.streams import Publisher, StreamMessage, StreamTopic
from xauusd_bot.common.schemas.journal import DecisionLogRecord
from xauusd_bot.common.runtime_config import get_ai_enabled, get_json
from xauusd_bot.common.service import make_publisher, run_consumer_service
from xauusd_bot.decision.pipeline import DecisionPipeline

log = structlog.get_logger(__name__)

GROUP = "decision-engine-v1"

# How long a fetched runtime AI flag is reused before re-reading Redis.
# Decisions arrive fast; a per-message GET would be wasteful, and a few
# seconds of staleness on an operator toggle is fine.
_AI_FLAG_TTL_SECONDS = 2.0


class _RuntimeAIFlag:
    """Cached reader for the dashboard-controlled AI-layer toggle.

    Reads :data:`runtime:ai_layer_enabled` from the trading Redis at most
    once per :data:`_AI_FLAG_TTL_SECONDS`, falling back to the static
    ``settings.ai_layer_enabled`` default when the key is unset or Redis
    is briefly unavailable.
    """

    def __init__(self, redis_client: Any, *, default: bool, ttl: float = _AI_FLAG_TTL_SECONDS) -> None:
        self._redis = redis_client
        self._default = default
        self._ttl = ttl
        self._value = default
        self._fetched_at = 0.0

    async def get(self) -> bool:
        now = time.monotonic()
        if now - self._fetched_at < self._ttl:
            return self._value
        try:
            self._value = await get_ai_enabled(self._redis, default=self._default)
        except Exception as exc:  # noqa: BLE001 - keep deciding on the last known value
            log.warning("decision_engine_ai_flag_read_failed", error=str(exc))
        self._fetched_at = now
        return self._value


def _ai_status(use_ai: bool, score_total, bundle, ai_info, threshold: float) -> str:
    """Why the AI did / didn't run for this decision (for the audit tab)."""
    if ai_info:                       # state:last_ai fresh → the LLM ran & answered
        return "ran"
    if not use_ai:
        return "ai_off"               # runtime dashboard toggle was off
    if score_total is None or score_total < threshold:
        return "score_low"            # below the LLM-consult threshold
    news = getattr(bundle, "news", None)
    if news is not None and getattr(news, "in_blackout_flag", False):
        return "news_blackout"        # hard news gate skips the LLM
    return "llm_error"                # consulted, but no valid response → rule fallback


def _decision_log(decision, score, qualification, symbol: str, ref_price, ai_info=None, ai_status=None) -> DecisionLogRecord:
    """Build the slim, persistence-shaped decision record (no feature bundle).

    ``ai_info`` is the fresh ``state:last_ai`` payload when the LLM ran for THIS
    decision (rationale / confidence / invalidations); None otherwise.
    """
    d = decision.model_dump(mode="json") if decision is not None else {}
    s = score.model_dump(mode="json") if score is not None else {}
    q = qualification.model_dump(mode="json") if qualification is not None else {}
    ts_raw = d.get("timestamp")
    try:
        ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.now(tz=UTC)
    except (TypeError, ValueError):
        ts = datetime.now(tz=UTC)
    ai = ai_info or {}
    conf = ai.get("confidence")
    return DecisionLogRecord(
        ts=ts,
        written_at=datetime.now(tz=UTC),
        symbol=symbol,
        action=str(d.get("action") or "no_trade"),
        direction=d.get("source_direction"),
        score=s.get("total_score"),
        band=s.get("band"),
        subscores={k: float(v) for k, v in (s.get("subscores") or {}).items() if v is not None},
        block_reason=d.get("block_reason"),
        qualified=bool(q.get("qualified")),
        entry_type=q.get("final_entry_type"),
        source_ai=bool(d.get("source_ai")),
        ref_price=float(ref_price) if ref_price is not None else None,
        ai_status=ai_status,
        ai_reasoning=ai.get("comment"),
        ai_confidence=float(conf) if conf is not None else None,
        ai_invalidations=list(ai.get("invalidations") or []),
    )


def _make_handler(pipeline: DecisionPipeline, publisher: Publisher, ai_flag: _RuntimeAIFlag, ai_redis=None):
    async def handle(msg: StreamMessage) -> None:
        ev = msg.payload
        assert isinstance(ev, FeaturesEvent)
        if ev.schema_version != ENVELOPE_SCHEMA_VERSION:
            log.warning("decision_engine_dropping_unknown_version", version=ev.schema_version)
            return
        use_ai = await ai_flag.get()
        decision, score, qualification = await pipeline.decide(
            ev.bundle, account=None, use_ai=use_ai
        )
        # The features stream is already compacted upstream; re-compact
        # (idempotent) so the decisions stream is bounded even if a future
        # path feeds in a full bundle. The execution-engine reads this
        # bundle for SL/TP.
        bundle = compact_bundle(
            ev.bundle,
            max_swings=pipeline.settings.bundle_compact_max_swings,
            max_mitigated_zones_per_tf=pipeline.settings.bundle_compact_max_mitigated_zones_per_tf,
        )
        await publisher.publish(
            StreamTopic.DECISIONS,
            DecisionEvent(
                symbol=ev.symbol,
                decision=decision,
                score=score,
                qualification=qualification,
                bundle=bundle,
                ref_price=ev.ref_price,
            ),
        )
        # Persist a slim decision record (no bundle) for the dashboard's
        # decision-history tab. Best-effort — never block the decision path.
        try:
            # Attach the LLM rationale only when the AI ran for THIS decision:
            # the OpenRouter client writes state:last_ai during decide(), so a
            # fresh (<20s) payload belongs to this bar; a stale one is a prior call.
            ai_info = None
            if ai_redis is not None:
                try:
                    last_ai = await get_json(ai_redis, "state:last_ai")
                    # ``written_at`` is real wall-clock; ``ts`` is broker bar time
                    # (for display) and must NOT be used to measure age.
                    stamp = (last_ai or {}).get("written_at") or (last_ai or {}).get("ts")
                    if last_ai and stamp:
                        age = (datetime.now(tz=UTC) - datetime.fromisoformat(stamp)).total_seconds()
                        if age <= 20:
                            ai_info = last_ai
                except Exception:  # noqa: BLE001
                    ai_info = None
            status = _ai_status(
                use_ai, getattr(score, "total_score", None), ev.bundle, ai_info,
                pipeline.settings.ai_layer_score_threshold,
            )
            await publisher.publish(
                StreamTopic.JOURNAL,
                JournalEvent(
                    symbol=ev.symbol,
                    entry_type="decision",
                    decision=_decision_log(decision, score, qualification, ev.symbol, ev.ref_price, ai_info, status),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - journaling is best-effort
            log.warning("decision_engine_journal_failed", error=str(exc))

    return handle


async def _run(settings: Settings) -> int:
    import redis.asyncio as aioredis

    # Runtime AI toggle + token-usage accounting live on the trading Redis
    # (same instance as the streams), so the dashboard sees both.
    flag_redis = aioredis.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    pipeline = DecisionPipeline(settings, usage_redis=flag_redis)
    publisher = make_publisher(settings)
    await publisher.connect()
    ai_flag = _RuntimeAIFlag(flag_redis, default=settings.ai_layer_enabled)
    log.info(
        "decision_engine_ai_config",
        ai_available=pipeline.ai_available,
        ai_default=settings.ai_layer_enabled,
        model=settings.openrouter_model,
    )
    handler = _make_handler(pipeline, publisher, ai_flag, ai_redis=flag_redis)

    async def _on_stop() -> None:
        await publisher.close()
        await flag_redis.aclose()

    return await run_consumer_service(
        ServiceRole.DECISION_ENGINE,
        settings,
        topic=StreamTopic.FEATURES,
        group=GROUP,
        model_cls=FeaturesEvent,
        handler=handler,
        on_stop=_on_stop,
        block_ms=settings.stream_block_ms,
        batch_size=settings.stream_batch_size,
    )


def main() -> int:
    settings = load_settings()
    setup_logging(level=settings.log_level)
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    raise SystemExit(main())
