"""Feature pipeline factory — the real engine stack, one call per bar.

Extracted from the in-process ``feature_smoke`` wiring so the
feature-engine *service* and the smokes share one source of truth for
how a :class:`FeatureSnapshotBundle` is assembled. This is the **real**
assembly (every engine's own output), not the synthetic-liquidity trick
the journal smoke uses to force trades on the toy sample.

I-3 (point-in-time): every engine receives the same ``ts`` cursor and
the bar list must already be PIT-filtered (``bar.time <= ts``). The
caller (feature-engine) guarantees this by only appending closed bars.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from xauusd_bot.common.schemas.features import FeatureSnapshotBundle
from xauusd_bot.connectors.schemas import Bar
from xauusd_bot.features._indicators import atr as compute_atr
from xauusd_bot.features._indicators import bars_to_df
from xauusd_bot.features.fvg import FVGEngine
from xauusd_bot.features.liquidity import LiquidityEngine
from xauusd_bot.features.momentum import CandleMomentumEngine
from xauusd_bot.features.news import NewsContextEngine, StubNewsProvider
from xauusd_bot.features.session import SessionEngine
from xauusd_bot.features.structure import MarketStructureEngine
from xauusd_bot.features.volume_range import FixedVolumeRangeEngine
from xauusd_bot.features.volume_trend import VolumeTrendEngine
from xauusd_bot.features.vwap import TripleVWAPEngine

log = structlog.get_logger(__name__)


class FeaturePipeline:
    """Holds the eight feature engines and assembles a bundle per bar.

    The engines are stateless; the pipeline re-calls ``.compute()`` with
    the running bar history each time. Construct once per process and
    reuse for every bar.
    """

    def __init__(self, *, news_provider: object | None = None) -> None:
        self.session = SessionEngine()
        self.vwap = TripleVWAPEngine()
        self.volume_range = FixedVolumeRangeEngine()
        self.fvg = FVGEngine()
        self.structure = MarketStructureEngine()
        self.momentum = CandleMomentumEngine()
        self.volume_trend = VolumeTrendEngine()
        self.liquidity = LiquidityEngine()
        self.news = NewsContextEngine(provider=news_provider or StubNewsProvider())

    def set_clock_offset(self, minutes: float) -> None:
        """Fan the broker→UTC offset out to every time-of-day-anchored engine.

        MT5 bar times are broker-server time (e.g. UTC+3). Session windows,
        the VWAP 00:00/07:00/12:00 anchors, the volume-profile period bounds
        and the news calendar are all defined in real UTC, so each must
        subtract this offset to classify/anchor correctly. 0 in replay/tests.
        """

        self.session.set_clock_offset(minutes)
        self.vwap.set_clock_offset(minutes)
        self.volume_range.set_clock_offset(minutes)
        self.news.set_clock_offset(minutes)

    def assemble(self, bars: list[Bar], ts: datetime) -> FeatureSnapshotBundle:
        """Run every engine over ``bars`` (PIT-filtered to ``ts``) and bundle it."""

        session_out = self.session.compute(bars, ts)
        vwap_out = self.vwap.compute(bars, ts)
        vr_out = self.volume_range.compute(bars, ts)
        fvg_out = self.fvg.compute(bars, ts)
        structure_out = self.structure.compute(bars, ts)
        momentum_out = self.momentum.compute(bars, ts)
        volume_trend_out = self.volume_trend.compute(bars, ts)
        close = float(bars[-1].close) if bars else 0.0
        liquidity_out = self.liquidity.compute(structure_out.liquidity_pools, close, bars, ts)
        news_out = self.news.compute(ts)

        atr_val: float | None = None
        if bars:
            try:
                atr_val = compute_atr(bars_to_df(bars), period=14)
            except Exception as exc:  # noqa: BLE001 - ATR is best-effort enrichment
                log.debug("feature_pipeline_atr_failed", error=str(exc))

        return FeatureSnapshotBundle(
            ts=ts,
            session=session_out,
            vwap=vwap_out,
            volume_range=vr_out,
            fvg=fvg_out,
            structure=structure_out,
            momentum=momentum_out,
            liquidity=liquidity_out,
            news=news_out,
            volume_trend=volume_trend_out,
            atr=atr_val,
            price=(close if bars else None),
        )


__all__ = ["FeaturePipeline"]
