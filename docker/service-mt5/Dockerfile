# Service image variant carrying the mt5linux RPyC client, for the
# connector-using services (data-collector / feature-engine / execution-engine)
# when running CONNECTOR_MODE=live with mt5_bridge_kind=mt5linux against the
# gmag11/metatrader5_vnc bridge (port 8001). The bot code itself is bind-mounted
# from /app/src by the compose layer — this image only adds the client library.
#
# mt5linux is installed with --no-deps because its declared dependencies include
# the Windows-only MetaTrader5 package (uninstallable on Linux). Its real runtime
# deps are added explicitly: rpyc pinned to 5.2.3 to match the bridge server
# (5.x and 6.x RPyC wire protocols are incompatible), plus plumbum.
#
# Build (on the VM):
#   docker build -f docker/service-mt5/Dockerfile -t xauusd-bot/service-mt5:0.1.0 .
FROM xauusd-bot/service:0.1.0
RUN pip install --no-cache-dir --no-deps mt5linux \
 && pip install --no-cache-dir "rpyc==5.2.3" plumbum
