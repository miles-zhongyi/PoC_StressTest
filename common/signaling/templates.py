"""
Template engine for realistic signalling messages.
===================================================

A *template* is a representative real signalling record (the common envelope plus
the per-message-type ``decoded`` body) in which a handful of per-instance envelope
leaves have been replaced by placeholder tokens of the form ``<<name>>``. At build
time `fill()` walks the structure and substitutes those tokens with live values,
leaving everything else — including the realistic ``decoded`` structure and any
opaque hex blobs taken from a real sample — verbatim.

Templates are produced offline by ``scripts/build_message_templates.py`` from the
decoded traces under ``22_decoded/`` and written to ``data/lte_templates.json``.
The catalog ships built-in defaults so the twin still runs when no index exists.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

# Envelope leaves abstracted into tokens when a template is extracted. Everything
# else (message_name, interface, protocol, serving_plmn, the whole decoded body) is
# kept verbatim, which is what makes the built message look real.
TEMPLATE_TOKENS = (
    "record_id",
    "timestamp",
    "procedure_id",
    "cell_id",
    "m_tmsi",
    "enb_ue_s1ap_id",
    "mme_ue_s1ap_id",
)


def tok(name: str) -> str:
    """The placeholder string used in a template for per-instance field ``name``."""
    return f"<<{name}>>"


def abstract_record(rec: dict) -> dict:
    """Turn one real decoded record into a template.

    Top-level per-instance fields in TEMPLATE_TOKENS become ``<<token>>`` strings;
    the ``decoded`` body and all other fields are copied verbatim (balanced fidelity:
    realistic structure, opaque hex preserved from the sample).
    """
    template = copy.deepcopy(rec)
    for field in TEMPLATE_TOKENS:
        if field in template:
            template[field] = tok(field)
    return template


def fill(template: dict, context: dict) -> dict:
    """Deep-copy ``template`` and replace any ``<<name>>`` token whose ``name`` is in
    ``context`` with the corresponding value. Tokens with no context value are left
    in place (which surfaces as an obvious marker if a field was forgotten)."""
    rev = {tok(k): v for k, v in context.items()}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and node in rev:
            return rev[node]
        return node

    return walk(template)


def load_templates(path: Path | str | None) -> dict[str, dict]:
    """Load a ``{message_name: template}`` map from JSON, or ``{}`` if unavailable."""
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
