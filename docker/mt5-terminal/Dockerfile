# mt5-terminal STUB.
#
# TODO: This is a placeholder for the production Wine + MT5 + Windows-Python
# RPyC bridge. It is NOT functional in block 1. Roadmap step 15
# (02_data_layer_mt5_bridge.md) wires the real implementation.
#
# Reference design (see 00_FINAL_PLAN.md §3.3):
#   - Base image: scottyhardy/docker-wine (or a maintained equivalent)
#   - Install MetaTrader 5 .exe into the Wine prefix at build time
#   - Install a Windows-Python 3.11 inside the prefix
#   - Boot a Windows-Python RPyC server that imports `MetaTrader5` and
#     exposes the connector surface to the Linux client. The RPyC server
#     runs sequentially because the MT5 API is not thread-safe.
#   - Expose a VNC port (127.0.0.1 only) for one-time Vantage account
#     login; never bind to 0.0.0.0.
#
# Healthcheck target: the RPyC bridge port (default 18812).

FROM python:3.11-slim
LABEL org.opencontainers.image.title="xauusd-bot/mt5-terminal" \
      org.opencontainers.image.description="STUB — Wine + MT5 + RPyC bridge for the XAUUSD bot" \
      org.opencontainers.image.licenses="MIT"

# This image intentionally has no functional MT5 setup yet. It exists so
# that docker-compose.prod.yml has a build target. A subsequent commit
# will replace this with the real Wine image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Placeholder process: tail the air so the container stays up so the
# rest of the stack can come up. Replace with the real bridge command
# in the follow-up commit.
CMD ["sh", "-c", "echo 'mt5-terminal STUB — see docker/mt5-terminal/Dockerfile TODO block'; trap : TERM INT; sleep 3600 & wait"]
