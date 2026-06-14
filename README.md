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
