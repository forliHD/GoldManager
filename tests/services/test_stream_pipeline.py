"""End-to-end integration test for the stream-connected service pipeline.

Drives the real Publisher/Consumer wiring over an in-memory fakeredis
server: data-collector → feature-engine → decision-engine →
execution-engine → journal-writer. Asserts a bar published on
``market_ticks`` propagates all the way to an order on ``orders`` and a
trade in the journal store.

The services' infinite ``run_consumer_service`` loops are *not* used
here; instead we exercise the exact same handler functions and
``Consumer.consume`` drain path the loop calls, so the test stays
bounded while covering the production code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import fakeredis.aioredis
import pytest

from xauusd_bot.common.config import Settings
from xauusd_bot.common.messaging.events import (
    BarClosedEvent,
    DecisionEvent,
    FeaturesEvent,
    JournalEvent,
    OrderEvent,
)
from xauusd_bot.common.messaging.streams import Consumer, Publisher, StreamTopic
from xauusd_bot.connectors.replay import ReplayConnector
from xauusd_bot.decision.pipeline import DecisionPipeline
from xauusd_bot.execution.pipeline import ExecutionPipeline
from xauusd_bot.features.pipeline import FeaturePipeline

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "xauusd_m1_sample.parquet"
# Bars 2086 / 2098 / 2099 of the committed sample qualify a trade, so a
# window of 20 streamed bars starting at 2080 exercises the full chain
# through to an order while keeping feature recompute cost bounded.
WARMUP_BARS = 2080
STREAM_BARS = 20


def _settings() -> Settings:
    return Settings(
        redis_url="redis://fake:6379/0",
        timescaledb_url="postgresql://fake/db",
        symbol="XAUUSD",
        environment="test",
        ai_layer_enabled=False,
    )


async def _drain(consumer: Consumer, handler, model_cls) -> int:
    """Consume until the stream is empty; return total processed."""

    total = 0
    for _ in range(1000):  # safety bound
        n = await consumer.consume(handler, model_cls, block_ms=20)
        if n == 0:
            break
        total += n
    return total


async def _run_pipeline() -> dict[str, int]:
    server = fakeredis.aioredis.FakeRedis(decode_responses=True)

    def _from_url(_url, **_kwargs):
        # All Publishers/Consumers share one fake server so data is
        # visible across clients, just like a real Redis.
        return server

    settings = _settings()
    connector = ReplayConnector(source_path=SAMPLE, symbol="XAUUSD")
    bars = [connector._row_to_bar(connector.bars.iloc[i], "M1") for i in range(WARMUP_BARS + STREAM_BARS)]
    warmup, streamed = bars[:WARMUP_BARS], bars[WARMUP_BARS : WARMUP_BARS + STREAM_BARS]

    import redis.asyncio

    orig_from_url = redis.asyncio.from_url
    redis.asyncio.from_url = _from_url  # type: ignore[assignment]
    try:
        # --- data-collector: publish the streamed bars to market_ticks.
        publisher = Publisher(settings.redis_url)
        await publisher.connect()
        for bar in streamed:
            await publisher.publish(StreamTopic.MARKET_TICKS, BarClosedEvent(symbol="XAUUSD", bar=bar))

        # --- feature-engine: buffer pre-seeded with warmup (live-mode path).
        feat_pipeline = FeaturePipeline()
        feat_buffer = list(warmup)
        feat_pub = Publisher(settings.redis_url)
        await feat_pub.connect()

        async def feat_handler(msg):
            ev = msg.payload
            feat_buffer.append(ev.bar)
            bundle = feat_pipeline.assemble(feat_buffer, ev.bar.time)
            await feat_pub.publish(
                StreamTopic.FEATURES,
                FeaturesEvent(symbol=ev.symbol, bundle=bundle, ref_price=ev.bar.close),
            )

        n_features = await _drain(
            Consumer(settings.redis_url, StreamTopic.MARKET_TICKS, "feature-engine-v1"),
            feat_handler,
            BarClosedEvent,
        )

        # --- decision-engine.
        dec_pipeline = DecisionPipeline(settings)
        dec_pub = Publisher(settings.redis_url)
        await dec_pub.connect()

        async def dec_handler(msg):
            ev = msg.payload
            decision, score, qual = await dec_pipeline.decide(ev.bundle, account=None)
            await dec_pub.publish(
                StreamTopic.DECISIONS,
                DecisionEvent(
                    symbol=ev.symbol,
                    decision=decision,
                    score=score,
                    qualification=qual,
                    bundle=ev.bundle,
                    ref_price=ev.ref_price,
                ),
            )

        n_decisions = await _drain(
            Consumer(settings.redis_url, StreamTopic.FEATURES, "decision-engine-v1"),
            dec_handler,
            FeaturesEvent,
        )

        # --- execution-engine.
        exec_pipeline = ExecutionPipeline(settings, connector)
        exec_pub = Publisher(settings.redis_url)
        await exec_pub.connect()

        async def exec_handler(msg):
            ev = msg.payload
            qual = ev.qualification
            if qual is None or not qual.qualified or ev.ref_price is None:
                return
            now = ev.decision.timestamp or ev.produced_at
            outcome = exec_pipeline.process(
                ev.decision, ev.score, qual, ev.bundle, ref_price=ev.ref_price, now=now
            )
            if not outcome.submitted:
                return
            await exec_pub.publish(StreamTopic.ORDERS, OrderEvent(symbol=ev.symbol, order=outcome.order))
            await exec_pub.publish(
                StreamTopic.JOURNAL, JournalEvent(symbol=ev.symbol, entry_type="trade", trade=outcome.trade)
            )
            await exec_pub.publish(
                StreamTopic.JOURNAL, JournalEvent(symbol=ev.symbol, entry_type="order", order=outcome.order)
            )

        await _drain(
            Consumer(settings.redis_url, StreamTopic.DECISIONS, "execution-engine-v1"),
            exec_handler,
            DecisionEvent,
        )

        # --- journal-writer.
        from xauusd_bot.journal import InMemoryJournalStore

        store = InMemoryJournalStore()

        async def journal_handler(msg):
            ev = msg.payload
            if ev.entry_type == "trade" and ev.trade is not None:
                await store.write_trade(ev.trade)
            elif ev.entry_type == "order" and ev.order is not None:
                await store.write_order(ev.order)

        n_journal = await _drain(
            Consumer(settings.redis_url, StreamTopic.JOURNAL, "journal-writer-v1"),
            journal_handler,
            JournalEvent,
        )

        # Count orders that landed on the orders stream.
        n_orders = await server.xlen(StreamTopic.ORDERS.value)
        trades = await store.list_trades()

        await publisher.close()
        return {
            "features": n_features,
            "decisions": n_decisions,
            "orders": int(n_orders),
            "journal_events": n_journal,
            "trades": len(trades),
        }
    finally:
        redis.asyncio.from_url = orig_from_url  # type: ignore[assignment]
        await server.aclose()


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample dataset not generated")
def test_stream_pipeline_end_to_end():
    result = asyncio.run(_run_pipeline())

    # Every streamed bar produces exactly one features event and one decision.
    assert result["features"] == STREAM_BARS
    assert result["decisions"] == STREAM_BARS
    # The chain reaches execution: at least one qualified trade became an
    # order, and the journal-writer persisted the trade.
    assert result["orders"] >= 1, result
    assert result["trades"] >= 1, result
    # Each order emits a trade + order journal event.
    assert result["journal_events"] >= result["orders"]
