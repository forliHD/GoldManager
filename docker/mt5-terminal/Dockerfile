# mt5-terminal — Wine + MetaTrader 5 + Windows-Python + RPyC bridge.
#
# This image bundles:
#   * Xvfb (headless X server) + xfce4 (minimal window manager) +
#     x11vnc (VNC server) + noVNC (browser-accessible VNC proxy)
#   * MetaTrader 5 terminal (Windows .exe, installed into the Wine prefix)
#   * Windows-Python 3.11 + rpyc (installed into the Wine prefix)
#   * mt5_bridge_server.py (the RPyC server that imports MetaTrader5)
#
# Build times:
#   * Cold build: ~10-15 min (Wine + MT5 download + Windows-Python).
#   * Incremental: ~1-2 min (only the bridge code layer changes).
#
# Reference: AGENTS.md §4h "Caveats aus Block 8".
#
# Base image: scottyhardy/docker-wine. We do NOT add Wine ourselves —
# the base image already provides it. If scottyhardy's image becomes
# unmaintained, the maintainer-equivalent is `sablen/docker-wine` or
# `accetto/ubuntu-vnc-xfce` (the latter is actively maintained; see
# AGENTS.md §4h).
#
# SECURITY: VNC is bound to 127.0.0.1 only via docker-compose.prod.yml.
# Never change `NOVNC_LISTEN=0.0.0.0` here without also pinning the
# Docker port to 127.0.0.1.

# ============================================================ stage 1: runtime
#
# Pull the wine base. We do not strip apt caches here because the build
# is cold-cache-frequent (MT5 installer URLs change).
FROM scottyhardy/docker-wine:latest

LABEL org.opencontainers.image.title="xauusd-bot/mt5-terminal" \
      org.opencontainers.image.description="Wine + MT5 + Windows-Python RPyC bridge for the XAUUSD bot (Block 8)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/forliHD/GoldManager" \
      org.opencontainers.image.version="0.8.0"

# --------------------------------------------------------------- ENV defaults
#
# All overridable via docker-compose.prod.yml or `docker run -e`.
# MT5_BRIDGE_PORT default 18812 matches the connector's default.
ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    XVFB_ARGS="-screen 0 1280x720x24" \
    WINEDEBUG=-all \
    MT5_BRIDGE_PORT=18812 \
    MT5_BRIDGE_HOST=0.0.0.0 \
    MT5_BRIDGE_LOG_LEVEL=INFO \
    MT5_INSTALLER_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" \
    PYTHON_WIN_INSTALLER_URL="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" \
    MT5_BRIDGE_AUTH_KEY="" \
    VNC_PASSWORD="changeme"

# --------------------------------------------------------------- system pkgs
#
# Layer 1: system packages. Kept in a single RUN to keep the image
# small. xvfb + x11vnc + novnc + websockify are the VNC stack.
# tini gives us proper signal handling (replaces the parent shell so
# `docker stop` actually reaches the wine / python processes).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        xvfb \
        x11vnc \
        xfce4 \
        xfce4-terminal \
        novnc \
        websockify \
        tini \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------- MT5 install
#
# Layer 2: MetaTrader 5 (Windows installer via Wine).
# The /S flag is the official silent-install flag (Inno Setup).
# We install into the default Wine prefix; the persistent volume
# (mt5-data) is mounted over /root/.wine in docker-compose.prod.yml.
#
# We download to /tmp first; the Wine prefix lives at /root/.wine
# and the MT5 installer self-installs into the prefix. The download
# can be retried on flaky mirrors.
ARG MT5_INSTALLER_URL=${MT5_INSTALLER_URL}
RUN mkdir -p /opt/mt5-bridge \
    && cd /opt/mt5-bridge \
    && echo "Downloading MT5 from ${MT5_INSTALLER_URL} ..." \
    && curl -fsSL --retry 3 --retry-delay 5 -o mt5setup.exe "${MT5_INSTALLER_URL}" \
    && echo "Installing MT5 via Wine (silent) ..." \
    # MT5's installer uses /auto for unattended install (NOT the Inno /S
    # flag — /S pops a hidden dialog under xvfb and hangs forever).
    # timeout guards against any remaining hang instead of stalling the build.
    && WINEPREFIX=/root/.wine WINEARCH=win64 timeout 600 xvfb-run -a \
        wine mt5setup.exe /auto \
    ; echo "MT5 install step exit=$? — verifying ..." \
    && ls -la /root/.wine/drive_c/Program\ Files/MetaTrader\ 5/terminal64.exe \
    && rm mt5setup.exe

# --------------------------------------------------------------- Python install
#
# Layer 3: Windows-Python 3.11 (silent install into the Wine prefix).
# `PrependPath=1` makes `wine python` resolvable from any directory.
ARG PYTHON_WIN_INSTALLER_URL=${PYTHON_WIN_INSTALLER_URL}
RUN cd /opt/mt5-bridge \
    && echo "Downloading Windows-Python 3.11 from ${PYTHON_WIN_INSTALLER_URL} ..." \
    && curl -fsSL --retry 3 --retry-delay 5 -o python-win.exe "${PYTHON_WIN_INSTALLER_URL}" \
    && echo "Installing Windows-Python via Wine (silent) ..." \
    && WINEPREFIX=/root/.wine WINEARCH=win64 xvfb-run -a \
        wine python-win.exe /quiet InstallAllUsers=1 PrependPath=1 \
            Include_pip=1 Include_launcher=0 \
    && rm python-win.exe \
    && echo "Verifying wine python ..." \
    && WINEPREFIX=/root/.wine WINEARCH=win64 xvfb-run -a \
        wine python --version

# --------------------------------------------------------------- pip + rpyc
#
# Layer 4: install rpyc into the Windows-Python. This is the actual
# bridge transport — the Linux side (LiveMT5Connector) talks RPyC to
# the Windows-Python side which in turn talks MetaTrader5.
#
# We pin rpyc to a known version (>=6.0) because the API changed
# between 5.x and 6.x. The bridge server targets 6.0.
RUN echo "Installing rpyc into Windows-Python via Wine ..." \
    && WINEPREFIX=/root/.wine WINEARCH=win64 xvfb-run -a \
        wine python -m pip install --upgrade pip \
    && WINEPREFIX=/root/.wine WINEARCH=win64 xvfb-run -a \
        wine python -m pip install "rpyc>=6.0,<7" \
    && echo "rpyc install done."

# --------------------------------------------------------------- bridge code
#
# Layer 5: copy the bridge server source. Bumping this layer is the
# common case in development (bridge code changes only).
COPY docker/mt5-terminal/mt5_bridge_server.py /opt/mt5-bridge/mt5_bridge_server.py

# --------------------------------------------------------------- supervisord
#
# Layer 6: process supervisor. We need four long-running processes:
#   * Xvfb (headless X server)
#   * x11vnc (VNC server bound to 127.0.0.1 via compose)
#   * websockify + noVNC (browser-accessible VNC, port 6080)
#   * wine mt5terminal64.exe (background, started after the X server)
#   * wine python mt5_bridge_server.py (the actual RPyC bridge)
COPY docker/mt5-terminal/supervisord.conf /etc/supervisor/conf.d/mt5-bridge.conf
RUN mkdir -p /var/log/supervisor

# --------------------------------------------------------------- entrypoint
#
# tini handles signals correctly (PID 1 init), then supervisord
# launches the rest. `restart: unless-stopped` in compose handles
# crashes; supervisord `autorestart=true` handles child crashes
# within a session.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]

# --------------------------------------------------------------- ports
#
# 18812: RPyC bridge (loopback-only in compose)
# 5900:  VNC (loopback-only in compose; one-time login)
# 6080:  noVNC web (loopback-only in compose)
EXPOSE 18812 5900 6080

# --------------------------------------------------------------- healthcheck
#
# Probes the RPyC bridge port (18812). A successful TCP connect means
# the bridge server is at least listening — but doesn't prove that
# mt5.initialize() succeeded. A deeper healthcheck would do an
# rpyc.connect + conn.root.exposed_ping() (if we add one), but that
# requires auth and adds latency. The current probe catches the
# common failure mode (container started but the wine process is
# still booting — start_period: 120s covers that).
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python3 -c "import socket; s=socket.socket(); s.connect(('127.0.0.1', 18812)); s.close()" || exit 1
