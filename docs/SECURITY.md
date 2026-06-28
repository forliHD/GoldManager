# Security notes

Tracking of security-relevant dependency advisories and the mitigations in
place for components that cannot yet be patched.

---

## rpyc — GHSA-h5cg-53g7-gqjw (HIGH): RCE via `numpy.array` on the server side

**Status:** mitigated by network isolation; upgrade **blocked** on upstream
`mt5linux`. Tracked in
[#8](https://github.com/forliHD/GoldManager/issues/8).

> **2026-06-28 — Dependabot alert #1 dismissed as `tolerable_risk`.** Re-verified
> the blocker still holds: `mt5linux` is at `1.0.3` and still hard-pins
> `rpyc==5.2.3`; no `rpyc>=6`-compatible release exists. The vulnerable side is
> the bundled *server*, so a client-side bump cannot fix it and would break the
> 5↔6-incompatible bridge protocol. Loopback-only publish
> (`127.0.0.1:8001:8001`) confirmed in `docker-compose.mt5.yml`. The alert will
> be re-opened and rpyc bumped in lockstep (see Resolution path below) once
> upstream ships `rpyc>=6` support.

### The advisory

[GHSA-h5cg-53g7-gqjw](https://github.com/tomerfiliba-org/rpyc/security/advisories/GHSA-h5cg-53g7-gqjw)
— *"Missing security check results in code execution when using `numpy.array`
on the server-side."*

- Affected range: `rpyc >=4.0.0, <6.0.0`. First patched version: `6.0.0`.
- Mechanism: an rpyc **server** that calls `np.array(x)` on a client-supplied
  netref triggers the object's remote `__array__` method during packet decoding
  (`_unbox → _netref_factory → class_factory`). A malicious **client** can use
  this to execute arbitrary code **on the server**, bypassing the
  `allow_pickle=False` default. The flaw is server-side and requires an
  untrusted client connecting to the server.

### Where it lives in this stack

The live MT5 bridge is an rpyc link:

- **Server (the vulnerable side):** the `mt5linux` rpyc server, run under
  wine-python inside the `gmag11/metatrader5_vnc` container on port `8001`
  (`scripts/mt5_bridge_up.sh`). It mirrors the MetaTrader5 API and returns
  numpy arrays for `copy_rates_*` / `copy_ticks_*`.
- **Client:** the bot's `Mt5LinuxConnector` (`service-mt5` image), connecting as
  `mt5-terminal:8001` over the compose network.

Both sides are pinned to `rpyc==5.2.3` (in the vulnerable range) — see the
`live` extra in [`pyproject.toml`](../pyproject.toml) and
[`docker/service-mt5/Dockerfile`](../docker/service-mt5/Dockerfile).

### Why we cannot bump to rpyc 6 yet

1. **`mt5linux` hard-pins `rpyc==5.2.3`.** The latest release, `mt5linux==1.0.3`
   (Feb 2026), declares `rpyc==5.2.3` as an exact dependency. No rpyc-6-compatible
   release exists. The vulnerable component is the *server* bundled with
   `mt5linux`, so it cannot be patched independently of an upstream release.
2. **rpyc 5 ↔ 6 is not a drop-in.** The 6.0.0 fix *"breaks backwards
   compatibility for those that rely on the `__array__` attribute used by numpy"*
   and may require `allow_pickle=True` to migrate. That `__array__`/numpy path is
   exactly the bridge's core data path, so a forced cross-version setup risks
   silently breaking live bar/tick transfer.
3. **Cannot be verified here.** `mt5linux` runs only inside the VM's wine
   container; a bump must be validated against the live bridge on
   `dev@192.168.178.192`, not on a dev/CI host.

A blind bump of the `live` pin would therefore break the bridge with no offsetting
benefit (the server stays on 5.2.3 regardless). We hold the pin until `mt5linux`
ships an rpyc-6-compatible release.

### Mitigation in place (network isolation)

The exploit requires an **untrusted client** reaching the server. In this stack
the bridge is **not exposed to the LAN**:

- The host publishes the bridge as **`127.0.0.1:8001:8001`** (loopback only) —
  [`docker-compose.mt5.yml`](../docker-compose.mt5.yml). It is reachable only
  from (a) the VM's own localhost and (b) containers on the compose network
  (the bot services). It is **not** reachable from other LAN hosts.
- The KasmVNC desktop (`3000`) is password-protected; set
  `MT5_VNC_BIND_HOST=127.0.0.1` and use an SSH tunnel for a stricter posture.

This matches the loopback-binding hardening already applied to Redis/TimescaleDB.

### Residual risk

- `mt5linux`'s built-in server performs **no connection authentication** (the
  `MT5_BRIDGE_AUTH_KEY` `on_connect` check exists only in the unused
  `docker/mt5-terminal/mt5_bridge_server.py`). Network isolation is the only
  control. A compromised container on the compose network, or a local process on
  the VM, could reach `8001` and trigger the RCE.
- The stack is still gated behind the dashboard emergency-stop
  (data-collection mode, no live orders), so the blast radius is bounded.

### Resolution path

Watch upstream `mt5linux` for a release supporting `rpyc>=6`. When it lands,
bump in lockstep — the `live` extra in `pyproject.toml`, the
`service-mt5` Dockerfile, and the rpyc pin in `scripts/mt5_bridge_up.sh` — then
verify the bridge reconnects to MT5 on the VM before considering it done.
