"""Static check for the mt5-terminal Docker image.

The actual Wine + MT5 + Windows-Python-RPyC image cannot be built
or tested in CI on macOS (it needs x86-Linux + Wine). What we *can*
do is read the Dockerfile and assert the canonical production
invariants:

* Base image is a known Wine base (scottyhardy, accetto/ubuntu-vnc-xfce,
  sablen/docker-wine, or the developer has documented an alternative
  in AGENTS.md §4h).
* Exposes the RPyC bridge port (18812).
* Has a HEALTHCHECK directive.
* No hardcoded passwords in ENV.
* The entrypoint boots the bridge server (not just a placeholder).
* The bridge server source is copied into the image (the file exists
  at the path referenced by the COPY directive).
* The companion supervisord.conf exists and references the bridge
  server.

These checks fail fast if someone replaces the production image with
a stub.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "mt5-terminal" / "Dockerfile"
SUPERVISORD = REPO_ROOT / "docker" / "mt5-terminal" / "supervisord.conf"
BRIDGE_SRC = REPO_ROOT / "docker" / "mt5-terminal" / "mt5_bridge_server.py"


# ============================================================ existence


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.exists(), f"Dockerfile not found: {DOCKERFILE}"
    assert DOCKERFILE.stat().st_size > 200, "Dockerfile looks like a stub"


def test_supervisord_conf_exists() -> None:
    assert SUPERVISORD.exists(), f"supervisord.conf not found: {SUPERVISORD}"


def test_bridge_server_source_exists() -> None:
    assert BRIDGE_SRC.exists(), f"bridge server source not found: {BRIDGE_SRC}"


# ============================================================ content


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text()


@pytest.fixture(scope="module")
def supervisord_text() -> str:
    return SUPERVISORD.read_text()


def test_dockerfile_uses_known_wine_base(dockerfile_text: str) -> None:
    """The base image must be a known Wine + X11 base.

    Acceptable base images (the parametrize-style list lives in
    ``_ACCEPTABLE_BASE_IMAGES``). If you change the base image to
    a different one, either add it to that list OR document the
    alternative in AGENTS.md §4h.
    """
    acceptable = [
        "scottyhardy/docker-wine",
        "accetto/ubuntu-vnc-xfce",
        "sablen/docker-wine",
        "dockurr/wine",
    ]
    matches = [b for b in acceptable if f"FROM {b}" in dockerfile_text]
    assert matches, (
        f"Expected FROM <known-wine-base> (one of {acceptable}); "
        f"first 5 lines:\n" + "\n".join(dockerfile_text.splitlines()[:5])
    )


def test_dockerfile_does_not_hardcode_passwords(dockerfile_text: str) -> None:
    """No `ENV FOO=bar` with a hardcoded password.

    A correct Dockerfile uses :literal:`${...}`-style indirection or
    an empty default that is overridden at runtime. Common
    password-like vars: PASSWORD, PASSWD, SECRET, KEY, TOKEN.
    """
    # Find every "ENV KEY=VALUE" (or "ENV KEY VALUE" syntax).
    env_lines = [l for l in dockerfile_text.splitlines() if re.match(r"\s*ENV\s+", l)]
    for line in env_lines:
        # Accept ${VAR} placeholders, but flag literal passwords.
        if re.search(r"(PASSWORD|PASSWD|SECRET|TOKEN)\s*=\s*[\"']?[A-Za-z0-9!@#$%^&*]+[\"']?",
                     line, re.IGNORECASE):
            # Allowed: a default that is clearly a placeholder
            # ("changeme", "", "***").
            if re.search(r"(changeme|REPLACE|CHANGE_ME|REDACTED|^\s*\"\")", line, re.IGNORECASE):
                continue
            pytest.fail(
                f"Dockerfile appears to hardcode a password: {line!r}. "
                f"Use ${{VAR}} indirection or set it via .env / compose environment."
            )


def test_dockerfile_exposes_bridge_port(dockerfile_text: str) -> None:
    """The RPyC bridge port 18812 must be EXPOSEd."""
    assert re.search(r"^\s*EXPOSE\s+(?:.*\s)?18812\b", dockerfile_text, re.MULTILINE), (
        "Dockerfile must EXPOSE 18812 (RPyC bridge port)"
    )


def test_dockerfile_has_healthcheck(dockerfile_text: str) -> None:
    """A HEALTHCHECK directive is required (compose uses it)."""
    assert "HEALTHCHECK" in dockerfile_text, "Dockerfile must declare a HEALTHCHECK"


def test_dockerfile_healthcheck_targets_bridge_port(dockerfile_text: str) -> None:
    """The HEALTHCHECK must probe the RPyC bridge port (18812)."""
    # Find the HEALTHCHECK ... CMD ... block.
    m = re.search(r"HEALTHCHECK[^\n]*\n((?:\s+[^\n]*\n)+)", dockerfile_text, re.MULTILINE)
    assert m, "no HEALTHCHECK body found"
    body = m.group(1)
    assert "18812" in body, f"HEALTHCHECK does not probe 18812; body:\n{body}"


def test_dockerfile_healthcheck_has_start_period(dockerfile_text: str) -> None:
    """Wine boot is slow. The HEALTHCHECK must have a generous
    start_period (>= 90s) so docker doesn't kill the container
    during MT5 boot."""
    # The HEALTHCHECK directive is on one line; the options
    # (--start-period, --interval, etc.) are on the same line in our
    # Dockerfile. We accept both layouts.
    hc_block = re.search(r"HEALTHCHECK[^\n]*", dockerfile_text)
    assert hc_block, "no HEALTHCHECK directive found"
    hc_text = hc_block.group(0)
    sp = re.search(r"--start-period=(\d+)s", hc_text)
    assert sp, f"HEALTHCHECK missing --start-period; line:\n{hc_text}"
    assert int(sp.group(1)) >= 90, f"start_period must be >= 90s (got {sp.group(1)}s)"


def test_dockerfile_copies_bridge_server(dockerfile_text: str) -> None:
    """The bridge server source must be COPYed into the image."""
    assert "COPY docker/mt5-terminal/mt5_bridge_server.py" in dockerfile_text, (
        "Dockerfile must COPY docker/mt5-terminal/mt5_bridge_server.py into the image"
    )


def test_dockerfile_installs_rpyc(dockerfile_text: str) -> None:
    """The image must pip-install rpyc into the Windows-Python."""
    assert "pip install" in dockerfile_text and "rpyc" in dockerfile_text, (
        "Dockerfile must pip install rpyc (the bridge transport)"
    )


def test_dockerfile_entrypoint_uses_supervisord(dockerfile_text: str) -> None:
    """The entrypoint must run supervisord, which boots Xvfb, VNC,
    noVNC, mt5terminal, and the bridge server. A pure shell
    placeholder is not acceptable."""
    m = re.search(r"^ENTRYPOINT\s+(.+)$", dockerfile_text, re.MULTILINE)
    assert m, "no ENTRYPOINT"
    cmd = m.group(1)
    assert "supervisord" in cmd or "supervisor" in cmd, (
        f"ENTRYPOINT does not use supervisord: {cmd!r}"
    )


def test_dockerfile_not_a_stub(dockerfile_text: str) -> None:
    """Catch-all: the old STUB Dockerfile had a TODO block and a
    trivial `tail -f` placeholder. This test makes the regression
    loud-failing."""
    assert "STUB" not in dockerfile_text, "Dockerfile still has STUB marker"
    assert "trap : TERM" not in dockerfile_text, "Dockerfile has STUB tail-the-air placeholder"
    assert "TODO" not in dockerfile_text, "Dockerfile still has TODO marker"


# ============================================================ supervisord


def test_supervisord_boots_bridge_server(supervisord_text: str) -> None:
    """supervisord.conf must launch the bridge server (the actual
    MT5 connector)."""
    assert "mt5_bridge_server.py" in supervisord_text, (
        "supervisord.conf does not reference mt5_bridge_server.py"
    )


def test_supervisord_boots_xvfb_and_vnc(supervisord_text: str) -> None:
    """The X server and VNC stacks are required for the MT5 GUI."""
    for program in ("xvfb", "x11vnc", "novnc", "mt5terminal"):
        assert f"[program:{program}]" in supervisord_text, (
            f"supervisord.conf is missing [{program}] block"
        )


def test_supervisord_x11vnc_is_loopback_only(supervisord_text: str) -> None:
    """VNC must be bound to localhost (defense in depth — compose
    also pins the port to 127.0.0.1)."""
    m = re.search(r"\[program:x11vnc\](.*?)(?=\[program:|\Z)", supervisord_text, re.DOTALL)
    assert m, "no [program:x11vnc] block"
    body = m.group(1)
    assert "-localhost" in body, f"x11vnc is not bound to localhost: {body!r}"


# ============================================================ security


def test_dockerfile_uses_tini_init(dockerfile_text: str) -> None:
    """tini is the standard PID-1 init for containers. It forwards
    signals to children so `docker stop` actually reaches the wine
    process (otherwise wine may eat the SIGTERM and you have to
    SIGKILL the container)."""
    assert "tini" in dockerfile_text, (
        "Dockerfile should use tini as PID-1 init (proper signal handling)"
    )
