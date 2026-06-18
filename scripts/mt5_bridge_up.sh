#!/bin/sh
# mt5_bridge_up.sh — (re)start the mt5linux RPyC bridge correctly inside the
# gmag11/metatrader5_vnc container. Run as the 'abc' user:
#   docker exec -u abc xauusd-mt5-terminal sh /config/mt5_bridge_up.sh
#
# Works around two gmag11 v2.3 bugs that break the built-in [7/7] bridge start:
#   1. start.sh launches the server with the removed `-w` flag (mt5linux 1.0.3
#      dropped it; the server must run UNDER wine-python instead).
#   2. Wine-python ships numpy 2.x, which breaks `import MetaTrader5` (5.0.36
#      was built against the numpy 1.x ABI).
WP=/config/.wine
# 0. ensure the KasmVNC web-auth file exists. The gmag11 v2.3 init does not
#    reliably create ${HOME}/.kasmpasswd from CUSTOM_USER/PASSWORD, so a fresh
#    /config volume yields a login that rejects all credentials. Recreate it
#    idempotently from the container env (CUSTOM_USER/PASSWORD).
if [ ! -s /config/.kasmpasswd ] && [ -n "${PASSWORD}" ]; then
  echo "[vnc] creating /config/.kasmpasswd for user ${CUSTOM_USER:-trader}..."
  printf "%s\n%s\n" "${PASSWORD}" "${PASSWORD}" \
    | kasmvncpasswd -u "${CUSTOM_USER:-trader}" -rwo /config/.kasmpasswd
fi
# 1. ensure numpy<2 in wine-python (idempotent; persisted in the /config volume)
if ! WINEPREFIX=$WP WINEDEBUG=-all wine python -c "import numpy,sys;sys.exit(0 if numpy.__version__[:2]=='1.' else 1)" 2>/dev/null; then
  echo "[bridge] installing numpy<2 in wine-python..."
  WINEPREFIX=$WP WINEDEBUG=-all wine python -m pip install --no-cache-dir "numpy<2"
fi
# 2. align the linux-side rpyc to the server's 5.2.3 (clients must match)
if ! python3 -c "import rpyc,sys;sys.exit(0 if rpyc.__version__=='5.2.3' else 1)" 2>/dev/null; then
  echo "[bridge] pinning linux rpyc==5.2.3..."
  pip install --user --break-system-packages --no-cache-dir "rpyc==5.2.3" >/dev/null 2>&1
fi
# 3. (re)start the mt5linux server under wine-python on the KasmVNC display :1
pkill -9 -f "wine python -m mt5linux" 2>/dev/null
sleep 1
cd /config
DISPLAY=:1 WINEPREFIX=$WP WINEDEBUG=-all nohup wine python -m mt5linux --host 0.0.0.0 -p 8001 > /config/mt5linux_wine.log 2>&1 &
sleep 8
if ss -tuln 2>/dev/null | grep -q ":8001 "; then echo "[bridge] OK — mt5linux listening on 0.0.0.0:8001"; else echo "[bridge] FAILED — see /config/mt5linux_wine.log"; fi
