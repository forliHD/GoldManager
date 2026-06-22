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
from xauusd_bot.features.fib import FibRetracementEngine
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

    def __init__(
        self,
        *,
        news_provider: object | None = None,
        fvg_extend_to_fractal: bool = True,
        fvg_extension_fractal_n: int = 2,
        fvg_extension_max_atr: float = 2.0,
    ) -> None:
        self.session = SessionEngine()
        self.vwap = TripleVWAPEngine()
        self.volume_range = FixedVolumeRangeEngine()
        self.fvg = FVGEngine(
            extend_to_fractal=fvg_extend_to_fractal,
            extension_fractal_n=fvg_extension_fractal_n,
            extension_max_atr=fvg_extension_max_atr,
        )
        self.structure = MarketStructureEngine()
        self.momentum = CandleMomentumEngine()
        self.volume_trend = VolumeTrendEngine()
        self.fib = FibRetracementEngine()
        self.liquidity = LiquidityEngine()
        self.news = NewsContextEngine(provider=news_provider or StubNewsProvider())

    def set_clock_offset(self, minutes: float) -> None:
        """Fan the broker→UTC offset out to the UTC-anchored engines.

        MT5 bar times are broker-server time (e.g. UTC+3). The session windows,
        the VWAP 00:00/07:00/12:00 anchors and the news calendar are defined in
        real UTC, so each subtracts this offset to classify/anchor correctly.

        The Volume Profile is the EXCEPTION: its Daily/Weekly/Monthly ranges are
        the BROKER's trading sessions (the strategy author trades off the broker
        daily candle: ~22:00→20:57 UTC = broker 00:00→23:57, week opens Sun 22:00
        UTC = broker Mon 00:00). Those are the broker CALENDAR periods, so
        ``volume_range`` stays at offset 0 (broker frame) — applying the UTC
        offset shifted every window by ~3h and produced wrong VAH/VPOC/VAL.
        Verified live: offset 0 reproduces the author's weekly (4365/4340/4265)
        and monthly (4685/4545/4430) levels; offset 180 did not.
        """

        self.session.set_clock_offset(minutes)
        self.vwap.set_clock_offset(minutes)
        self.news.set_clock_offset(minutes)
        # NOT volume_range — see docstring (broker-calendar periods).

    def assemble(
        self, bars: list[Bar], ts: datetime, vp_bars: list[Bar] | None = None
    ) -> FeatureSnapshotBundle:
        """Run every engine over ``bars`` (PIT-filtered to ``ts``) and bundle it.

        ``vp_bars`` is an optional, deeper bar history used **only** for the
        Volume Profile. The locked Daily/Weekly/Monthly profiles need weeks/months
        of bars, but the other engines (esp. FVG, which is ~O(n²)) must stay on a
        short window or assembly blows past the 1-bar/minute budget. volume_range
        itself is cheap even over deep history (~0.7s/80k bars). Defaults to
        ``bars`` (backtest/tests pass the full replay window directly).
        """

        session_out = self.session.compute(bars, ts)
        vwap_out = self.vwap.compute(bars, ts)
        vr_out = self.volume_range.compute(vp_bars if vp_bars is not None else bars, ts)
        fvg_out = self.fvg.compute(bars, ts)
        structure_out = self.structure.compute(bars, ts)
        momentum_out = self.momentum.compute(bars, ts)
        volume_trend_out = self.volume_trend.compute(bars, ts)
        fib_out = self.fib.compute(bars, ts)
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
            fib=fib_out,
            atr=atr_val,
            price=(close if bars else None),
        )


__all__ = ["FeaturePipeline"]
