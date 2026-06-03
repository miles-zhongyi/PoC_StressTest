"""
Format-fidelity check: does each twin-generated message match the structure of the
real 22_decoded record of the same type?

The twin will later run on production data, so its messages must be structurally
identical to the decoded traces. This compares the *shape* (nested keys / list-vs-
scalar), ignoring per-instance values (filled tokens) and the twin-only sidecar
(`_twin`, `txn`) which real signalling never carries.

`build_fidelity_report(catalog, get_real_by_name)` returns a per-message-type report
used by tests/test_fidelity.py and the dashboard's /api/fidelity panel.
"""
from __future__ import annotations

from . import procedures as proc

# Twin-only top-level additions (transport correlation + simulation sidecar).
TWIN_ONLY = {"_twin", "txn"}


def shape(obj):
    """Structural skeleton: dict -> {key: shape}, list -> [shape(first)] or [], scalar -> typename."""
    if isinstance(obj, dict):
        return {k: shape(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(obj[0])] if obj else []
    return type(obj).__name__


def compare(twin_msg: dict, real_rec: dict) -> dict:
    """Structural comparison of a twin message against a real trace record."""
    twin = {k: v for k, v in twin_msg.items() if k not in TWIN_ONLY}
    extra = sorted(set(twin) - set(real_rec))        # envelope keys twin has, real lacks
    missing = sorted(set(real_rec) - set(twin))      # envelope keys real has, twin lacks
    decoded_match = shape(twin.get("decoded")) == shape(real_rec.get("decoded"))
    return {
        "match": not extra and not missing and decoded_match,
        "extra_fields": extra,
        "missing_fields": missing,
        "decoded_match": decoded_match,
    }


def flow_logicals() -> list[str]:
    """Distinct logical messages the twin emits across the whole call flow."""
    seen: set[str] = set()
    out: list[str] = []
    for step in proc.ALL_STEPS:
        for logical in (step.uplink, step.downlink):
            if logical not in seen:
                seen.add(logical)
                out.append(logical)
    if proc.RRC_CONNECTION_REJECT not in seen:        # reject is an admit-failure reply
        out.append(proc.RRC_CONNECTION_REJECT)
    return out


def build_fidelity_report(catalog, get_real_by_name) -> dict:
    """For each flow message type: build the twin message and compare it to the real
    22_decoded record of the *same message_name* (``get_real_by_name(name) -> rec|None``).
    Types absent from the capture report ``no-sample`` (cannot be validated)."""
    rows = []
    for logical in flow_logicals():
        msg = catalog.build(logical, ue_id="ue-fidelity-00000", cell="cell-1",
                            position={"x": 0, "y": 0}, tx_power_dbm=23.0, demand_mbps=0.03)
        name = msg.get("message_name")
        real = get_real_by_name(name)
        row = {
            "logical": logical,
            "message_name": name,
            "record_id": msg.get("record_id"),
            "interface": msg.get("interface"),
        }
        if not real:
            row["status"] = "no-sample"
        else:
            cmp = compare(msg, real)
            row["status"] = "ok" if cmp["match"] else "diff"
            row.update(cmp)
        rows.append(row)
    return {
        "rows": rows,
        "total": len(rows),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "diff": sum(1 for r in rows if r["status"] == "diff"),
        "no_sample": sum(1 for r in rows if r["status"] == "no-sample"),
    }
