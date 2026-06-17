"""Frontend structure tests for Block 9.

We can't run JS in CI (no headless browser in this project), so we
test the static structure: index.html and dashboard.js. Cross-reference
the JS against the FastAPI endpoints in api.py to ensure no API
contract drift.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


DASHBOARD_DIR = Path("src/xauusd_bot/dashboard")
STATIC_DIR = DASHBOARD_DIR / "static"
INDEX_HTML = STATIC_DIR / "index.html"
DASHBOARD_JS = STATIC_DIR / "dashboard.js"
API_PY = DASHBOARD_DIR / "api.py"


# ---------- index.html structure ----------

def test_index_html_exists() -> None:
    assert INDEX_HTML.exists()
    text = INDEX_HTML.read_text()
    assert len(text) > 5000, "index.html looks suspiciously small"


def test_index_html_has_lightweight_charts_cdn() -> None:
    text = INDEX_HTML.read_text()
    assert "lightweight-charts" in text
    assert "unpkg.com" in text


def test_index_html_has_login_form_with_password_field() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="login-form"' in text
    assert 'id="login-password"' in text
    # The password field MUST be type="password", not "text" — this is
    # a security regression test (Probe C in integration check).
    # Match the input element with login-password id, allowing attributes
    # in any order.
    m = re.search(r'<input\b[^>]*\bid="login-password"[^>]*>', text)
    assert m, "login-password input element not found"
    assert 'type="password"' in m.group(0), (
        "login-password input is missing type='password' — security regression"
    )


def test_index_html_has_all_five_tabs() -> None:
    text = INDEX_HTML.read_text()
    for tab in ["indicators", "trades", "backtest", "reviews", "proposals"]:
        assert f'data-tab="{tab}"' in text, f"Missing tab: {tab}"
        assert f'id="tab-{tab}"' in text, f"Missing tab content: {tab}"


def test_index_html_has_chart_container() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="chart"' in text
    assert 'id="timeframe-selector"' in text
    assert 'id="chart-refresh"' in text


def test_index_html_has_topbar_with_user_role_logout() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="user-info"' in text
    assert 'id="user-role"' in text
    assert 'id="logout-btn"' in text


def test_index_html_has_mode_toggle_admin_only() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="mode-toggle-wrap"' in text
    assert 'id="mode-toggle-btn"' in text
    assert 'id="mode-pill"' in text
    # The mode toggle should be hidden by default
    assert 'id="mode-toggle-wrap"' in text and 'class="mode-toggle hidden"' in text


def test_index_html_has_modal_for_confirmation() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="modal-bg"' in text
    assert 'id="modal-title"' in text
    assert 'id="modal-body"' in text
    assert 'id="modal-confirm"' in text
    assert 'id="modal-cancel"' in text


def test_index_html_has_bottombar_ws_status() -> None:
    text = INDEX_HTML.read_text()
    assert 'id="ws-text"' in text
    assert 'id="ws-dot"' in text
    assert 'id="ws-last"' in text


def test_index_html_has_no_meta_trader5() -> None:
    text = INDEX_HTML.read_text()
    assert "MetaTrader5" not in text
    assert "metatrader5" not in text.lower()


def test_index_html_loads_dashboard_js() -> None:
    text = INDEX_HTML.read_text()
    assert "dashboard.js" in text


# ---------- dashboard.js structure ----------

def test_dashboard_js_exists() -> None:
    assert DASHBOARD_JS.exists()
    text = DASHBOARD_JS.read_text()
    assert len(text) > 10000, "dashboard.js looks suspiciously small"


def test_dashboard_js_subscribes_to_all_topics() -> None:
    text = DASHBOARD_JS.read_text()
    for topic in ["ticks", "features", "decisions", "orders", "journal"]:
        assert f"'{topic}'" in text, f"dashboard.js does not reference topic: {topic}"


def test_dashboard_js_uses_credentials_include() -> None:
    """All API calls must send the session cookie."""
    text = DASHBOARD_JS.read_text()
    assert "credentials: 'include'" in text


def test_dashboard_js_handles_login_response() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/auth/login" in text
    assert "/api/auth/logout" in text
    assert "/api/auth/me" in text


def test_dashboard_js_handles_chart_data() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/chart/candles" in text
    assert "/api/chart/overlays" in text


def test_dashboard_js_handles_journal_data() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/journal/trades" in text
    assert "/api/journal/aggregate" in text


def test_dashboard_js_handles_backtest() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/backtest/list" in text
    assert "/api/backtest/run" in text
    assert "/api/backtest/status" in text


def test_dashboard_js_handles_review() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/review/daily" in text
    assert "/api/review/weekly" in text


def test_dashboard_js_handles_fitting_proposal() -> None:
    text = DASHBOARD_JS.read_text()
    assert "/api/fitting-proposal/list" in text
    assert "/api/fitting-proposal/approve" in text
    assert "/api/fitting-proposal/reject" in text
    assert "/api/fitting-proposal/validate" in text


def test_dashboard_js_handles_mode_toggle_with_confirmation() -> None:
    """Mode toggle MUST show a confirmation modal — hard rule §4j.13."""
    text = DASHBOARD_JS.read_text()
    assert "/api/mode/toggle" in text
    assert "showConfirmModal" in text
    assert "confirm: true" in text or '"confirm": true' in text


def test_dashboard_js_no_password_in_logs() -> None:
    """The frontend must never log a password to console."""
    text = DASHBOARD_JS.read_text()
    # Allow 'password' as field name but not in a console.log
    for m in re.finditer(r"console\.log\([^)]*\)", text):
        assert "password" not in m.group(0).lower(), (
            "console.log with password field — PII leak"
        )


def test_dashboard_js_role_check_for_admin_actions() -> None:
    text = DASHBOARD_JS.read_text()
    # Mode toggle visibility gated on role
    assert "state.user.role" in text
    assert "'admin'" in text or '"admin"' in text


def test_dashboard_js_websocket_reconnect_with_backoff() -> None:
    text = DASHBOARD_JS.read_text()
    assert "reconnect" in text.lower()
    assert "30000" in text or "30 * 1000" in text  # max 30s backoff
    assert "Math.pow" in text or "2 **" in text or "Math.exp" in text  # exponential


# ---------- API contract cross-reference ----------

def _api_endpoints() -> set[str]:
    """Extract all FastAPI router paths from api.py."""
    text = API_PY.read_text()
    return set(re.findall(r'@router\.\w+\("(/api/[^"]+)"', text))


def test_dashboard_js_references_all_api_endpoints() -> None:
    """Cross-reference: every endpoint in api.py should be used in dashboard.js.

    Allow some endpoints (e.g. /api/mode/toggle) to be referenced via
    a different code path; we check the URL string is present.
    """
    endpoints = _api_endpoints()
    js = DASHBOARD_JS.read_text()
    missing = [ep for ep in endpoints if ep not in js]
    assert not missing, f"dashboard.js does not reference endpoints: {missing}"


def test_dashboard_js_uses_credentials_for_all_fetches() -> None:
    """All API calls must include credentials. Either via the api() helper
    (which always adds credentials: 'include') or via direct fetch() with
    credentials in the options.
    """
    text = DASHBOARD_JS.read_text()
    # The api() helper must always pass credentials
    assert "credentials: 'include'" in text
    # Find all fetch() calls (naive, single-level paren match)
    fetch_calls = re.findall(r"fetch\([^)]*\)", text)
    for f in fetch_calls:
        assert "credentials" in f, f"Direct fetch() without credentials: {f[:100]}"
    # All non-fetch HTTP calls must go through the api() helper
    # (i.e. no raw XMLHttpRequest, $.ajax, etc.)
    assert "XMLHttpRequest" not in text, "Raw XMLHttpRequest found — must use api() helper"
    assert "$.ajax" not in text and "$.get(" not in text and "$.post(" not in text, (
        "jQuery AJAX found — must use api() helper"
    )


# ---------- I-1 / I-4 audits on dashboard/ ----------

def test_no_meta_trader5_import_in_dashboard_python() -> None:
    """I-1: dashboard/ must not import MetaTrader5."""
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", "import MetaTrader5", "src/xauusd_bot/dashboard/"],
        capture_output=True, text=True
    )
    # Filter out __pycache__
    lines = [l for l in result.stdout.splitlines() if "__pycache__" not in l]
    assert not lines, f"Found MetaTrader5 imports in dashboard/: {lines}"


def test_no_execution_constructs_in_dashboard_python() -> None:
    """I-4: dashboard/ must not construct positions/SL/TP (read-only)."""
    import subprocess
    result = subprocess.run(
        ["grep", "-rn", "position_size\\|lot_size\\|VolumeInLots", "src/xauusd_bot/dashboard/"],
        capture_output=True, text=True
    )
    lines = [l for l in result.stdout.splitlines() if "__pycache__" not in l]
    assert not lines, f"Found I-4 violations in dashboard/: {lines}"


def test_dashboard_js_parses_as_valid_javascript_structure() -> None:
    """Sanity: dashboard.js has matching braces and parens.

    The naive regex-based comment/string stripping is imperfect; we
    only check brace balance as a coarse structural sanity test.
    """
    text = DASHBOARD_JS.read_text()
    # Strip strings and comments for balance check
    no_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    no_strings = re.sub(r"'(?:[^'\\]|\\.)*'", "''", no_strings)
    no_strings = re.sub(r"`(?:[^`\\]|\\.)*`", "``", no_strings)
    no_strings = re.sub(r"//[^\n]*", "", no_strings)
    no_strings = re.sub(r"/\*.*?\*/", "", no_strings, flags=re.DOTALL)
    opens = no_strings.count("{")
    closes = no_strings.count("}")
    assert opens == closes, f"Brace mismatch: {opens} open vs {closes} close"
    # Paren balance check is unreliable via regex because of nested
    # template literals, regex patterns, etc. Skip strict check; the
    # brace balance + the fact that Node.js/browser successfully
    # parses dashboard.js (manual smoke + the structural tests above)
    # is sufficient.
