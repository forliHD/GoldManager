# XAUUSD Trading Bot

Vantage-MT5 → Python Feature-Engine → AI-Decision-Layer (OpenRouter/MiniMax BYOK) → Risk/Execution → Journal/Review.

This is the **block-1 skeleton**: repo structure, Docker stack, connector abstraction (Replay / Live / Paper),
data layer (OHLC, Spread, Quality), common layer (config, schemas, Redis Streams, JSON logging),
synthetic XAUUSD-M1 sample dataset, and a replay smoke CLI.

See `00_FINAL_PLAN.md` for the full architecture.

---

## Repository layout

```
GoldManager/
├── docker-compose.base.yml         # redis, timescaledb, all Python services
├── docker-compose.dev.yml          # Mac: CONNECTOR_MODE=replay, no MT5
├── docker-compose.prod.yml         # Ubuntu: + mt5-terminal (Wine)
├── pyproject.toml
├── .env.example
├── tools/
│   └── generate_sample_data.py     # deterministic 30d XAUUSD M1 sample
├── data/
│   └── sample/
│       └── xauusd_m1_sample.parquet
├── docker/
│   ├── service/Dockerfile          # shared Python service image
│   └── mt5-terminal/Dockerfile     # Wine + MT5 + RPyC bridge (STUB)
├── src/xauusd_bot/
│   ├── connectors/                 # IMarketConnector + Replay / Live / Paper / Safety
│   ├── data/                       # OHLCBuilder, SpreadMonitor, DataQualityMonitor, SymbolSpecLoader
│   ├── features/                   # (placeholders for block 2)
│   ├── decision/                   # (placeholders for block 4)
│   ├── execution/                  # (placeholders for block 4)
│   ├── journal/                    # (placeholders for block 4)
│   ├── review/                     # (placeholders for block 4)
│   ├── viz/                        # (placeholders for block 4)
│   ├── cli/
│   │   └── replay_smoke.py         # smoke CLI: replay 10k M1 bars end-to-end
│   └── common/
│       ├── config/settings.py      # Pydantic-Settings
│       ├── schemas/                # Pydantic schemas
│       ├── messaging/              # Redis Streams wrapper
│       └── logging/                # structlog setup
└── tests/                          # pytest
```

---

## Quickstart (Replay mode, no MT5 required)

```bash
# 1. Set up Python 3.11+ environment
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 2. Generate (or refresh) the sample M1 dataset
.venv/bin/python -m tools.generate_sample_data

# 3. Run the replay smoke CLI (10k bars end-to-end)
.venv/bin/python -m xauusd_bot.cli.replay_smoke
# → writes logs/replay_smoke.json
cat logs/replay_smoke.json
```

> **Heads-up:** some macOS Python 3.14 venvs have a macOS-`UF_HIDDEN` flag
> on `site-packages/` that prevents editable `.pth` files from loading.
> If `python -m xauusd_bot.cli.replay_smoke` fails with
> `ModuleNotFoundError: No module named 'xauusd_bot'`, either:
>
> 1. Use `PYTHONPATH=src .venv/bin/python -m xauusd_bot.cli.replay_smoke`
> 2. Or `pip install --no-build-isolation -e ".[dev]"` to bypass the .pth
> 3. Or rebuild the venv at a path that doesn't carry the hidden flag.
>
> `pytest` is unaffected because `pyproject.toml` sets `pythonpath = ["src"]`.

## Quickstart (Docker, dev)

```bash
cp .env.example .env
# edit OPENROUTER_API_KEY if you want to exercise the AI layer later

docker compose -f docker-compose.base.yml -f docker-compose.dev.yml up
```

`docker-compose.dev.yml` excludes the `mt5-terminal` service and overrides
`CONNECTOR_MODE=replay` so the full pipeline runs on macOS without Wine.

## Quickstart (Docker, prod)

On an Ubuntu VM with Wine-capable kernel:

```bash
cp .env.example .env  # fill in MT5_LOGIN, MT5_PASSWORD, OPENROUTER_API_KEY
docker compose -f docker-compose.base.yml -f docker-compose.prod.yml up
```

The `mt5-terminal` container is **a STUB in this block** — wiring it up against
`scottyhardy/docker-wine` and a Vantage account is task 15 of the roadmap.

---

## Quickstart (Backtest, Block 5b)

The BacktestEngine replays historical M1 bars through the same Replay → Features
→ Decision → Qualification → Risk → Size → Stops → Order → Journal → KPI pipeline
that the bot will use live. WalkForwardEngine then rolls In-Sample / Out-of-Sample
windows to detect overfitting.

```bash
# 1. Single backtest (no walk-forward), ~20s on 300 M1 bars
PYTHONPATH=src REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.backtest_smoke \
    --start-date 2026-04-15 --end-date 2026-04-30 \
    --warmup-bars 200 --max-bars 300

# 2. With WalkForward (rolling IS/OOS windows), ~3 min on 1-month sample data
#    Note: total range must be >= in_sample + out_of_sample. With 7d/3d/3d the
#    30d sample yields ~7 windows.
PYTHONPATH=src REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.backtest_smoke \
    --start-date 2026-04-01 --end-date 2026-04-30 \
    --warmup-bars 100 --max-bars 200 \
    --in-sample-days 7 --out-of-sample-days 3 --step-days 3
```

Both commands write `logs/backtest_snapshot.json` with `n_bars_processed`,
`n_trades`, `stats` (sharpe, sortino, max_dd, profit_factor, expectancy),
`equity_curve_sample`, `r_distribution`, `setup_breakdown`, and — when
walk-forward is enabled — `wf_windows`, `wf_oos_degradation`, `wf_is_overfit`.

**Caveats:**
- The shipped `data/sample/xauusd_m1_sample.parquet` is **synthetic**; the
  backtest CLI injects synthetic TP zones for the same reason as `journal_smoke`
  (real liquidity engines need real volume clusters). Production data
  (Dukascopy / Vantage export) bypasses this hack automatically.
- Slippage and spread models are simplified: `FixedSlippage`, `VolatilitySlippage`,
  `FixedSpread`, `VolatilitySpread`, `NewsAwareSpread` — see `src/xauusd_bot/backtest/models.py`.
  Realistic execution modeling (variable per bar, vol-correlated, news-impact)
  is on the Block-5c / Demo-Forward backlog.
- WalkForward flags `is_overfit=true` when OOS-Sharpe degrades >30% vs IS-Sharpe.
  This is a heuristic, not a verdict — review `wf_windows` and the per-window
  stats in `backtest_snapshot.json` before drawing conclusions.
- `--max-bars` caps the inner per-bar cost in `BacktestEngine` (default 1500).
  The CLI defaults to 200 for a fast smoke; raise it for production backtests.

---

## Architecture invariants (enforced)

1. **No `MetaTrader5` import outside `connectors/live.py`.** The `MetaTrader5` package
   is Windows-only and not available on Mac. Dev runs entirely on `ReplayConnector`
   + `PaperBroker`; prod uses `LiveMT5Connector` over a Wine/RPyC bridge.
2. **Identical schemas** between `ReplayConnector` and `LiveMT5Connector` — proven
   by `tests/test_schemas.py`.
3. **Point-in-Time:** `ReplayConnector` returns only data with `time <= current_t`.
   The `advance_time(t)` method moves the cursor forward; the smoke CLI exercises this.
4. **Brain vs Hands:** the AI layer (block 4) never computes position size, SL, or TP —
   `RiskManager` and `PositionSizer` are authoritative.
5. **Tick-volume is relative only.** Consumers (feature engines) MUST treat it as a
   percentile / z-score, never as an absolute signal.

## Build roadmap

See `00_FINAL_PLAN.md` §9. This commit covers steps **1 + 2 + 3** (skeleton, replay,
data layer). Subsequent blocks will fill in features, decision, execution, journal,
review, and AI layer.

## Disclaimer

This is a software project, not financial advice. Run on demo until you've validated
the pipeline end-to-end.
