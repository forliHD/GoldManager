"""Offline exit-param sweep — replay recorded exit tapes (Part B).

Entries come from the LLM (expensive, deterministic given the bundles); exits
are deterministic post-entry. So we record a *tape* during ONE LLM backtest
(:meth:`BacktestEngine.dump_tape`) and replay the multi-tier exit logic over it
with ANY exit config — sweeping exit params in seconds, no LLM, no feature
recomputation, no VM load.

The replay mirrors :meth:`BacktestEngine._walk_open_positions` exactly (SL →
weekend flat → TP1/TP2 partials → TP3/runner → trail with BE-floor + structure +
chandelier), reusing the real :class:`StopManager` / :class:`TakeProfitManager`,
so a swept R reproduces what the live engine would have produced. A regression
test pins this fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from xauusd_bot.common.schemas.features import (
    FeatureSnapshotBundle,
    MarketStructureOutput,
    StructureEvent,
    StructureEventType,
    SwingPoint,
)
from xauusd_bot.connectors.schemas import OrderSide, SymbolSpec
from xauusd_bot.decision.trading_hours import TradingWindow
from xauusd_bot.execution.stops import StopManager
from xauusd_bot.execution.take_profit import TakeProfitManager


@dataclass(frozen=True)
class ExitConfig:
    """One point in the exit-parameter sweep grid (mirrors the exec_* settings)."""

    be_trigger_r: float = 1.0
    chandelier_atr: float = 3.0
    trail_buffer_atr: float = 0.5
    trail_activate_r: float = 1.0
    min_sl_atr: float = 0.6
    min_sl_points: float = 3.0
    tp1_pct: float = 30.0
    tp2_pct: float = 30.0
    tp3_pct: float = 40.0
    weekend_flat: bool = True
    weekend_flat_utc: str = "20:55"
    label: str = ""


@dataclass
class ReplayStats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    r_list: list[float] = field(default_factory=list)

    @property
    def winrate(self) -> float:
        c = self.wins + self.losses
        return self.wins / c if c else 0.0

    @property
    def avg_r(self) -> float:
        return self.total_r / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> float:
        gp = sum(r for r in self.r_list if r > 0)
        gl = -sum(r for r in self.r_list if r < 0)
        return gp / gl if gl > 0 else float("inf") if gp > 0 else 0.0


def _win(cfg: ExitConfig) -> TradingWindow:
    class _C:
        exec_trading_window_enabled = True
        exec_trading_timezone = "UTC"
        exec_trading_start_local = "00:00"
        exec_trading_end_local = "22:55"
        exec_weekend_flat_enabled = cfg.weekend_flat
        exec_weekend_flat_utc = cfg.weekend_flat_utc

    return TradingWindow.from_settings(_C())


def _bundle(rec: dict) -> FeatureSnapshotBundle:
    ts = datetime.fromisoformat(rec["ts"])
    swings: list[SwingPoint] = []
    if rec.get("swing_low") is not None:
        swings.append(SwingPoint(kind="low", price=rec["swing_low"], time=ts, bar_index=0, is_external=True))
    if rec.get("swing_high") is not None:
        swings.append(SwingPoint(kind="high", price=rec["swing_high"], time=ts, bar_index=0, is_external=True))

    def _ev(v):
        if not v:
            return None
        # should_close_runner only reads .type; the other fields are dummies.
        return StructureEvent(
            type=StructureEventType(v), level=0.0, close=0.0, distance_atr=0.0, time=ts, bar_index=0
        )

    return FeatureSnapshotBundle(
        ts=ts,
        atr=rec.get("atr") or 0.0,
        broker_offset_minutes=rec.get("offset", 0.0),
        structure=MarketStructureOutput(
            swings=swings, last_bos=_ev(rec.get("last_bos")), last_choch=_ev(rec.get("last_choch")),
            liquidity_pools=[], trend="range", fractal_n=2,
        ),
    )


def replay_entry(entry: dict, bars: list[dict], cfg: ExitConfig, spec: SymbolSpec) -> float:
    """Replay one trade's exit management over its bar tape → R multiple."""

    stop = StopManager(
        spec=spec, min_sl_atr=cfg.min_sl_atr, min_sl_points=cfg.min_sl_points,
        trail_buffer_atr=cfg.trail_buffer_atr, chandelier_atr=cfg.chandelier_atr,
    )
    tp_mgr = TakeProfitManager(spec=spec, tp1_pct=cfg.tp1_pct, tp2_pct=cfg.tp2_pct, tp3_pct=cfg.tp3_pct)
    win = _win(cfg)
    contract = spec.trade_contract_size or Decimal("100")
    step = spec.volume_step if spec.volume_step and spec.volume_step > 0 else Decimal("0.01")
    vmin = spec.volume_min or Decimal("0.01")

    side = OrderSide.BUY if entry["side"] == "long" else OrderSide.SELL
    is_long = side == OrderSide.BUY
    sign = Decimal("1") if is_long else Decimal("-1")
    entry_px = Decimal(entry["entry_price"])
    sl = Decimal(entry["initial_sl"])
    tp1 = Decimal(entry["tp1"]) if entry.get("tp1") else None
    tp2 = Decimal(entry["tp2"]) if entry.get("tp2") else None
    tp3 = Decimal(entry["tp3"]) if entry.get("tp3") else None
    risk = Decimal(entry["risk_amount"]) if Decimal(entry["risk_amount"]) > 0 else Decimal("1")
    init_vol = Decimal(entry["initial_volume"])
    init_risk = abs(entry_px - sl) or Decimal("1")
    realized = Decimal("0")
    volume = init_vol
    peak = entry_px
    tp1_taken = tp2_taken = armed = False

    def _partial(frac: float, price: Decimal) -> None:
        nonlocal realized, volume
        vol = (init_vol * Decimal(str(frac)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        if vol < vmin or vol >= volume:
            return
        realized += (price - entry_px) * sign * vol * contract
        volume -= vol

    def _final_r(price: Decimal) -> float:
        return float((realized + (price - entry_px) * sign * volume * contract) / risk)

    # Manage from the bar AFTER entry (entry bar = bars[0]).
    for rec in bars[1:]:
        high, low, close = Decimal(rec["high"]), Decimal(rec["low"]), Decimal(rec["close"])
        # 1. SL.
        if (is_long and low <= sl) or (not is_long and high >= sl):
            return _final_r(sl)
        # 2. Weekend flat.
        if win.should_flatten_for_weekend(datetime.fromisoformat(rec["ts"]), rec.get("offset", 0.0)):
            return _final_r(close)
        bundle = _bundle(rec)
        # 3. TP1 / TP2 partials.
        if not tp1_taken and tp1 is not None and ((is_long and high >= tp1) or (not is_long and low <= tp1)):
            _partial(cfg.tp1_pct / 100.0, tp1)
            tp1_taken = armed = True
        if not tp2_taken and tp2 is not None and ((is_long and high >= tp2) or (not is_long and low <= tp2)):
            _partial(cfg.tp2_pct / 100.0, tp2)
            tp2_taken = True
        # 4. TP3 / runner.
        if tp3 is not None and ((is_long and high >= tp3) or (not is_long and low <= tp3)):
            return _final_r(tp3)
        if armed and tp3 is not None and tp_mgr.should_close_runner(side, tp3, close, bundle)[0]:
            return _final_r(close)
        # 5. Track peak, arm, trail (BE floor + structure + chandelier).
        fav = high if is_long else low
        peak = max(peak, fav) if is_long else min(peak, fav)
        if not armed and cfg.trail_activate_r > 0 and (fav - entry_px) * sign >= init_risk * Decimal(str(cfg.trail_activate_r)):
            armed = True
        if armed:
            excursion = (peak - entry_px) * sign
            be_armed = tp1_taken or (cfg.be_trigger_r > 0 and excursion >= init_risk * Decimal(str(cfg.be_trigger_r)))
            new_sl = stop.trail(side, sl, entry_px, bundle, peak=peak, be_armed=be_armed).sl_price
            if new_sl is not None and ((is_long and new_sl > sl) or (not is_long and new_sl < sl)):
                sl = new_sl
    # Window/horizon end → close remaining at the last close.
    return _final_r(Decimal(bars[-1]["close"])) if len(bars) > 1 else 0.0


def replay_tape(tape: dict, cfg: ExitConfig, spec: SymbolSpec) -> ReplayStats:
    """Replay all entries in a tape with ``cfg`` → aggregate :class:`ReplayStats`."""

    bars = tape["bars"]
    stats = ReplayStats()
    for entry in tape["entries"]:
        i = entry["entry_bar_index"]
        r = replay_entry(entry, bars[i:], cfg, spec)
        stats.n += 1
        stats.total_r += r
        stats.r_list.append(r)
        if r > 1e-9:
            stats.wins += 1
        elif r < -1e-9:
            stats.losses += 1
    return stats


__all__ = ["ExitConfig", "ReplayStats", "replay_entry", "replay_tape"]
