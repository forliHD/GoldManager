"""Exit-replay fidelity — the offline replay reproduces the engine's R."""

from __future__ import annotations

import pytest

from xauusd_bot.backtest.exit_replay import ExitConfig, replay_entry, replay_tape
from tests._execution_factories import make_symbol_spec


def _rec(ts, h, lo, c, atr=2.0, **kw):
    return {
        "ts": ts, "high": str(h), "low": str(lo), "close": str(c), "atr": atr,
        "swing_low": kw.get("swing_low"), "swing_high": kw.get("swing_high"),
        "last_bos": kw.get("last_bos"), "last_choch": kw.get("last_choch"), "offset": 0.0,
    }


_ENTRY = {
    "side": "long", "entry_price": "2000", "initial_sl": "1990",
    "tp1": "2010", "tp2": "2020", "tp3": "2030",
    "risk_amount": "1000", "initial_volume": "1.0", "entry_bar_index": 0,
}


def test_replay_reproduces_multitier_2_1r():
    # Same scenario as tests/backtest/test_phase_d_exits: TP1(1R)+TP2(2R)+
    # runner-to-TP3(3R) @ 30/30/40 → blended 2.1R. Must match the live engine.
    bars = [
        _rec("2026-04-15T13:00:00+00:00", 2000, 2000, 2000),  # entry bar (skipped)
        _rec("2026-04-15T13:01:00+00:00", 2012, 2001, 2011),  # TP1
        _rec("2026-04-15T13:02:00+00:00", 2022, 2010, 2021),  # TP2
        _rec("2026-04-15T13:03:00+00:00", 2032, 2020, 2031),  # TP3 → runner closes
    ]
    r = replay_entry(_ENTRY, bars, ExitConfig(), make_symbol_spec())
    assert r == pytest.approx(2.1, abs=0.05)


def test_be_floor_turns_a_reversal_into_a_scratch_not_a_loss():
    # Price spikes to +1R (arms BE), then reverses straight back through entry.
    # With the BE floor the runner exits ~break-even, NOT at the original −1R SL.
    bars = [
        _rec("2026-04-15T13:00:00+00:00", 2000, 2000, 2000),
        _rec("2026-04-15T13:01:00+00:00", 2012, 2000, 2002),  # TP1 hit (+1R touch), arms BE
        _rec("2026-04-15T13:02:00+00:00", 2002, 1985, 1986),  # reverses through entry → BE floor stops it
    ]
    r_be = replay_entry(_ENTRY, bars, ExitConfig(be_trigger_r=1.0), make_symbol_spec())
    r_no_be = replay_entry(_ENTRY, bars, ExitConfig(be_trigger_r=0.0, chandelier_atr=0.0), make_symbol_spec())
    assert r_be > r_no_be          # the BE floor protects the runner
    assert r_be > -0.5             # not a full loss
    assert r_no_be < r_be          # without BE the 70% runner rides to the original SL


def test_replay_tape_aggregates():
    tape = {
        "bars": [
            _rec("2026-04-15T13:00:00+00:00", 2000, 2000, 2000),
            _rec("2026-04-15T13:01:00+00:00", 2012, 2001, 2011),
            _rec("2026-04-15T13:02:00+00:00", 2022, 2010, 2021),
            _rec("2026-04-15T13:03:00+00:00", 2032, 2020, 2031),
        ],
        "entries": [dict(_ENTRY)],
    }
    stats = replay_tape(tape, ExitConfig(), make_symbol_spec())
    assert stats.n == 1 and stats.wins == 1
    assert stats.total_r == pytest.approx(2.1, abs=0.05)
