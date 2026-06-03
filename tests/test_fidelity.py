"""
Format-fidelity: every twin message type that exists in 22_decoded must be
structurally identical to the real decoded record (ignoring filled token values and
the twin-only `_twin`/`txn` fields). Message types absent from the capture are
reported as 'no-sample', not failed.

Needs the sample cache (data/trace_message_samples.json) built by the dashboard's
TraceCatalog / scripts; the test skips cleanly if it is missing.
"""
import json
import os
from pathlib import Path

import pytest

from common.signaling import get_catalog
from common.signaling.fidelity import build_fidelity_report

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("TRACE_SAMPLES_CACHE", ROOT / "data" / "trace_message_samples.json"))
os.environ.setdefault("LTE_TEMPLATES", str(ROOT / "data" / "lte_templates.json"))


def _real_by_name_from_cache():
    if not CACHE.is_file():
        pytest.skip(f"sample cache not built: {CACHE}")
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    by_record = data.get("by_record") or {}
    by_name = {}
    for payload in by_record.values():
        name = payload.get("message_name")
        if name and name not in by_name:
            by_name[name] = payload.get("message")
    if not by_name:
        pytest.skip("sample cache is empty")
    return by_name


def test_present_message_types_are_format_identical():
    by_name = _real_by_name_from_cache()
    report = build_fidelity_report(get_catalog("lte"), lambda n: by_name.get(n))

    validated = [r for r in report["rows"] if r["status"] != "no-sample"]
    assert validated, "no flow message type had a real sample to validate against"

    mismatches = [r for r in validated if r["status"] != "ok"]
    detail = "\n".join(
        f"  {r['message_name']}: extra={r.get('extra_fields')} "
        f"missing={r.get('missing_fields')} decoded_match={r.get('decoded_match')}"
        for r in mismatches
    )
    assert not mismatches, f"twin messages diverge from real 22_decoded format:\n{detail}"


def test_core_attach_messages_have_real_samples():
    """Guard against a vacuous pass: the common attach/release types should be present."""
    by_name = _real_by_name_from_cache()
    report = build_fidelity_report(get_catalog("lte"), lambda n: by_name.get(n))
    status = {r["message_name"]: r["status"] for r in report["rows"]}
    for name in ("RRC_RRC_CONNECTION_SETUP_COMPLETE", "RRC_MEASUREMENT_REPORT",
                 "S1_INITIAL_CONTEXT_SETUP_REQUEST", "S1_UE_CONTEXT_RELEASE_REQUEST"):
        assert status.get(name) == "ok", f"{name} should validate as ok, got {status.get(name)}"
