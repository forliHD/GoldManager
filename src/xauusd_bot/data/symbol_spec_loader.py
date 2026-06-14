"""Symbol spec loader — read :class:`SymbolSpec` from the connector with caching.

The :class:`SymbolSpecLoader` is a thin layer that ensures downstream
modules always have a :class:`SymbolSpec` without re-asking the
connector on every call. Spec drift is detected by comparing the
re-loaded spec to a hash of the previous one.
"""

from __future__ import annotations

import hashlib
import json
from typing import Callable

import structlog

from xauusd_bot.connectors.schemas import SymbolSpec

log = structlog.get_logger(__name__)


class SymbolSpecLoader:
    """Cache and version :class:`SymbolSpec` objects."""

    def __init__(self, fetch: Callable[[str], SymbolSpec]) -> None:
        self._fetch = fetch
        self._cache: dict[str, SymbolSpec] = {}
        self._hashes: dict[str, str] = {}

    def get(self, symbol: str) -> SymbolSpec:
        """Return the spec for ``symbol``, fetching if absent."""

        if symbol not in self._cache:
            self._cache[symbol] = self._fetch(symbol)
            self._hashes[symbol] = self._hash(self._cache[symbol])
            log.info("symbol_spec_loaded", symbol=symbol, hash=self._hashes[symbol])
        return self._cache[symbol]

    def refresh(self, symbol: str) -> tuple[SymbolSpec, bool]:
        """Re-fetch ``symbol``. Returns (spec, changed)."""

        old_hash = self._hashes.get(symbol)
        new_spec = self._fetch(symbol)
        new_hash = self._hash(new_spec)
        changed = old_hash is not None and old_hash != new_hash
        self._cache[symbol] = new_spec
        self._hashes[symbol] = new_hash
        if changed:
            log.warning("symbol_spec_drift", symbol=symbol, old=old_hash, new=new_hash)
        return new_spec, changed

    @staticmethod
    def _hash(spec: SymbolSpec) -> str:
        # Deterministic JSON dump (sorted keys, default=str for Decimal/datetime).
        payload = json.loads(json.dumps(spec.model_dump(), default=str, sort_keys=True))
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]
