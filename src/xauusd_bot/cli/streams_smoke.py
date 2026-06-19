"""Streams smoke CLI — verify the stream-connected pipeline end to end.

Publishes a window of sample bars onto ``market_ticks`` and drains them
through the feature-engine → decision-engine → execution-engine →
journal-writer handlers against the configured Redis (``REDIS_URL``, or
``--redis-url``). Reports per-topic message counts and the resulting
trade count, then writes ``logs/streams_smoke.json``.

Run against a real Redis (e.g. the compose ``redis`` service)::

    python -m xauusd_bot.cli.streams_smoke --redis-url redis://localhost:6379/0

This exercises the same Publisher/Consumer wiring and pipeline factories
the services use, without the infinite consume loops — a bounded check
suitable for CI and container bring-up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make the package importable from a bare checkout (mirrors the other CLIs).
_THIS = Path(__file__).resolve()
_SRC = _THIS.parents[3]
if str(_SRC) not in sys.path and (_SRC / "xauusd_bot").exists():
    sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

from xauusd_bot.common.config import Settings  # noqa: E402
from xauusd_bot.common.logging import setup_logging  # noqa: E402
from xauusd_bot.common.messaging.events import (  # noqa: E402
    BarClosedEvent,
    DecisionEvent,
    FeaturesEvent,
    JournalEvent,
    OrderEvent,
)
from xauusd_bot.common.messaging.streams import Consumer, Publisher, StreamTopic  # noqa: E402
from xauusd_bot.connectors.replay import ReplayConnector  # noqa: E402
from xauusd_bot.decision.pipeline import DecisionPipeline  # noqa: E402
from xauusd_bot.execution.pipeline import ExecutionPipeline  # noqa: E402
from xauusd_bot.features.pipeline import FeaturePipeline  # noqa: E402
from xauusd_bot.journal import InMemoryJournalStore  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_SAMPLE = _SRC.parent / "data" / "sample" / "xauusd_m1_sample.parquet"
DEFAULT_REPORT = _SRC.parent / "logs" / "streams_smoke.json"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="End-to-end streams smoke.")
    p.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    p.add_argument("--warmup-bars", type=int, default=2080)
    p.add_argument("--n-bars", type=int, default=20)
    p.add_argument("--symbol", type=str, default="XAUUSD")
    p.add_argument("--redis-url", type=str, default=None, help="Override REDIS_URL.")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    return p.parse_args(argv)


async def _drain(consumer: Consumer, handler, model_cls) -> int:
    total = 0
    for _ in range(10_000):
        n = await consumer.consume(handler, model_cls, block_ms=50)
        if n == 0:
            break
        total += n
    await consumer.close()
    return total


async def _run(args: argparse.Namespace, settings: Settings) -> dict:
    connector = ReplayConnector(source_path=args.sample, symbol=args.symbol)
    total = args.warmup_bars + args.n_bars
    bars = [connector._row_to_bar(connector.bars.iloc[i], "M1") for i in range(total)]  # noqa: SLF001
    warmup, streamed = bars[: args.warmup_bars], bars[args.warmup_bars : total]
    url = settings.redis_url

    pub = Publisher(url)
    await pub.connect()
    for bar in streamed:
        await pub.publish(StreamTopic.MARKET_TICKS, BarClosedEvent(symbol=args.symbol, bar=bar))

    feat = FeaturePipeline()
    buf = list(warmup)
    fpub = Publisher(url)
    await fpub.connect()

    async def feat_h(msg):
        ev = msg.payload
        buf.append(ev.bar)
        bundle = feat.assemble(buf, ev.bar.time)
        await fpub.publish(StreamTopic.FEATURES, FeaturesEvent(symbol=ev.symbol, bundle=bundle, ref_price=ev.bar.close))

    n_features = await _drain(Consumer(url, StreamTopic.MARKET_TICKS, "feature-engine-smoke"), feat_h, BarClosedEvent)

    dec = DecisionPipeline(settings)
    dpub = Publisher(url)
    await dpub.connect()

    async def dec_h(msg):
        ev = msg.payload
        decision, score, qual = await dec.decide(ev.bundle, account=None)
        await dpub.publish(
            StreamTopic.DECISIONS,
            DecisionEvent(symbol=ev.symbol, decision=decision, score=score, qualification=qual, bundle=ev.bundle, ref_price=ev.ref_price),
        )

    n_decisions = await _drain(Consumer(url, StreamTopic.FEATURES, "decision-engine-smoke"), dec_h, FeaturesEvent)

    ep = ExecutionPipeline(settings, connector)
    epub = Publisher(url)
    await epub.connect()

    async def exec_h(msg):
        ev = msg.payload
        qual = ev.qualification
        if qual is None or not qual.qualified or ev.ref_price is None:
            return
        now = ev.decision.timestamp or ev.produced_at
        out = ep.process(ev.decision, ev.score, qual, ev.bundle, ref_price=ev.ref_price, now=now)
        if not out.submitted:
            return
        await epub.publish(StreamTopic.ORDERS, OrderEvent(symbol=ev.symbol, order=out.order))
        await epub.publish(StreamTopic.JOURNAL, JournalEvent(symbol=ev.symbol, entry_type="trade", trade=out.trade))
        await epub.publish(StreamTopic.JOURNAL, JournalEvent(symbol=ev.symbol, entry_type="order", order=out.order))

    await _drain(Consumer(url, StreamTopic.DECISIONS, "execution-engine-smoke"), exec_h, DecisionEvent)

    store = InMemoryJournalStore()

    async def jrn_h(msg):
        ev = msg.payload
        if ev.entry_type == "trade" and ev.trade is not None:
            await store.write_trade(ev.trade)
        elif ev.entry_type == "order" and ev.order is not None:
            await store.write_order(ev.order)

    n_journal = await _drain(Consumer(url, StreamTopic.JOURNAL, "journal-writer-smoke"), jrn_h, JournalEvent)
    trades = await store.list_trades()
    await pub.close()
    await fpub.close()
    await dpub.close()
    await epub.close()

    return {
        "redis_url": url,
        "bars_streamed": len(streamed),
        "features": n_features,
        "decisions": n_decisions,
        "journal_events": n_journal,
        "trades": len(trades),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    setup_logging(level="INFO")
    if not args.sample.exists():
        print(f"ERROR: sample not found at {args.sample}", file=sys.stderr)
        return 2
    settings = Settings(environment="test")  # type: ignore[call-arg]
    if args.redis_url:
        settings = settings.model_copy(update={"redis_url": args.redis_url})
    try:
        report = asyncio.run(_run(args, settings))
    except Exception as exc:  # noqa: BLE001 - smoke surfaces the error and exits non-zero
        log.error("streams_smoke_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    ok = report["features"] == report["bars_streamed"] and report["trades"] >= 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
