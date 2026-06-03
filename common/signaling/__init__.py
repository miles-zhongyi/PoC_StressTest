"""
Pluggable signalling layer for the digital twin.

`get_catalog()` returns the message catalog for the configured radio technology
(``RADIO_TECH``: ``lte`` today, ``nr`` reserved for 5G). The catalog turns logical
flow steps into realistic, template-built messages and classifies received messages
back to a step. See catalog.py and procedures.py for the model.
"""
from __future__ import annotations

import os

from .catalog import SignalingCatalog, twin
from .lte import LteCatalog
from .nr import NrCatalog

_CATALOGS = {"lte": LteCatalog, "nr": NrCatalog}
_cache: dict[tuple[str, str], SignalingCatalog] = {}


def get_catalog(tech: str | None = None, templates_path: str | None = None) -> SignalingCatalog:
    """Return a cached catalog for ``tech`` (default from RADIO_TECH env, else lte).

    ``templates_path`` defaults to the ``LTE_TEMPLATES`` env var; a missing file
    simply means the catalog falls back to its built-in DEFAULT_TEMPLATES.
    """
    tech = (tech or os.environ.get("RADIO_TECH", "lte")).strip().lower()
    if templates_path is None:
        templates_path = os.environ.get("LTE_TEMPLATES", "")
    key = (tech, templates_path or "")
    if key not in _cache:
        cls = _CATALOGS.get(tech)
        if cls is None:
            raise ValueError(f"unknown RADIO_TECH={tech!r} (known: {', '.join(_CATALOGS)})")
        _cache[key] = cls(templates_path=templates_path or None)
    return _cache[key]


__all__ = ["get_catalog", "SignalingCatalog", "twin"]
