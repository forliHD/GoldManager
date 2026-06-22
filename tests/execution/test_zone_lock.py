"""Tests for the ZoneRegistry — one entry per zone with the strategy lifecycle."""

from __future__ import annotations

from xauusd_bot.execution.zone_lock import ZoneRegistry, band_from_price


def test_empty_allows_entry():
    r = ZoneRegistry()
    assert r.can_enter("short", 4312.0) is True


def test_open_blocks_second_entry_same_band():
    # The 3-in-3-min cluster: second/third entry into the same band is blocked.
    r = ZoneRegistry()
    r.open("short", 4310.0, 4314.0)
    assert r.can_enter("short", 4312.0) is False  # inside band, still open
    assert r.can_enter("short", 4311.0) is False


def test_different_side_not_blocked():
    r = ZoneRegistry()
    r.open("short", 4310.0, 4314.0)
    assert r.can_enter("long", 4312.0) is True


def test_price_outside_band_allowed():
    r = ZoneRegistry()
    r.open("short", 4310.0, 4314.0)
    assert r.can_enter("short", 4300.0) is True  # different zone


def test_closed_zone_still_blocks_until_price_leaves():
    # BE/scratch close keeps the zone → no immediate re-fire on the same touch.
    r = ZoneRegistry()
    zid = r.open("short", 4310.0, 4314.0)
    r.close(zid)
    assert r.can_enter("short", 4312.0) is False  # 'used' still blocks


def test_rearms_after_price_leaves_band():
    r = ZoneRegistry()
    zid = r.open("short", 4310.0, 4314.0)
    r.close(zid)
    r.note_price(4330.0)  # price left the band → fresh re-test possible
    assert r.can_enter("short", 4312.0) is True


def test_h1_close_above_kills_short_zone():
    # A supply/short zone dies when an H1 candle closes ABOVE it.
    r = ZoneRegistry()
    zid = r.open("short", 4310.0, 4314.0)
    r.close(zid)
    r.on_h1_close(4320.0)  # H1 close above the zone → invalidated
    assert r.can_enter("short", 4312.0) is False  # 'dead' blocks permanently
    r.note_price(4330.0)  # leaving the band does NOT revive a dead zone
    assert r.can_enter("short", 4312.0) is False


def test_h1_close_below_kills_long_zone():
    r = ZoneRegistry()
    zid = r.open("long", 4136.0, 4140.0)
    r.close(zid)
    r.on_h1_close(4130.0)  # H1 close below the demand zone → invalidated
    assert r.can_enter("long", 4138.0) is False


def test_h1_close_inside_zone_does_not_kill():
    r = ZoneRegistry()
    zid = r.open("short", 4310.0, 4314.0)
    r.close(zid)
    r.on_h1_close(4312.0)  # close still inside → zone NOT dead
    r.note_price(4330.0)
    assert r.can_enter("short", 4312.0) is True


def test_open_absorbs_overlapping_armed_zone():
    r = ZoneRegistry()
    zid = r.open("short", 4310.0, 4314.0)
    r.close(zid)
    r.note_price(4330.0)  # → armed
    # A fresh re-test opens a new position; the stale armed zone is absorbed.
    r.open("short", 4310.0, 4314.0)
    armed = [z for z in r.zones if z.status == "armed"]
    assert armed == []


def test_registry_is_bounded_and_never_evicts_open_zones():
    # Live never reset()s the registry → it must stay bounded and must never
    # drop a live ('open') zone while ageing out old non-open history.
    r = ZoneRegistry(max_zones=8)
    live = r.open("long", 4100.0, 4101.0)  # stays 'open' the whole time
    for i in range(50):  # churn many dead zones well past the cap
        zid = r.open("short", 5000.0 + i, 5001.0 + i)
        r.close(zid)
        r.on_h1_close(9999.0)  # H1 close above → 'dead'
    assert len(r.zones) <= 8
    # The live position's zone survived and still blocks its band.
    assert any(z.id == live and z.status == "open" for z in r.zones)
    assert r.can_enter("long", 4100.5) is False


def test_band_from_price_atr():
    low, high = band_from_price(4312.0, atr=4.0, atr_mult=0.5, min_half=0.5)
    assert low == 4310.0 and high == 4314.0  # half = 0.5*4 = 2
    # ATR missing → min_half floor.
    low2, high2 = band_from_price(4312.0, atr=None, min_half=0.5)
    assert low2 == 4311.5 and high2 == 4312.5
