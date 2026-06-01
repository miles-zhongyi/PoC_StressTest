"""
Parse decoded call-trace JSON (large arrays) and map records to twin events.

Trace files are huge (100k+ records per file). Use iter_trace_records() for
streaming; run scripts/build_trace_index.py once to build a compact JSONL index
for fast time-ordered replay.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Map trace labels → twin signalling (common/protocol.py message types)
_ATTACH = frozenset({
    "RRC_RRC_CONNECTION_REQUEST",
    "rrcConnectionRequest",
    "RRC_RRC_CONNECTION_SETUP",
    "rrcConnectionSetup",
    "RRC_RRC_CONNECTION_SETUP_COMPLETE",
    "rrcConnectionSetupComplete",
    "S1_INITIAL_CONTEXT_SETUP_REQUEST",
    "S1_INITIAL_CONTEXT_SETUP_RESPONSE",
})
_RELEASE = frozenset({
    "RRC_RRC_CONNECTION_RELEASE",
    "rrcConnectionRelease",
    "S1_UE_CONTEXT_RELEASE_REQUEST",
    "S1_UE_CONTEXT_RELEASE_COMMAND",
    "S1_UE_CONTEXT_RELEASE_COMPLETE",
})
_MEASUREMENT = frozenset({
    "measurementReport",
    "RRC_MEASUREMENT_REPORT",
})
_SKIP = frozenset({
    "S1_CELL_TRAFFIC_TRACE",
    "counterCheckResponse",
})

_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:\d{2}|Z)?$"
)


def parse_timestamp(ts: str) -> float:
    """ISO-8601 trace timestamp → Unix seconds (UTC)."""
    if not ts:
        return 0.0
    m = _ISO_TS.match(ts.strip())
    if not m:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    base, frac, tz = m.groups()
    if frac:
        frac = (frac + "000000")[:7]
        text = f"{base}{frac}{tz or '+00:00'}"
    else:
        text = f"{base}{tz or '+00:00'}"
    return datetime.fromisoformat(text).timestamp()


def _label(rec: dict) -> str:
    meta = rec.get("decoding_metadata") or {}
    return (
        rec.get("message_name")
        or meta.get("decoded_message_choice")
        or meta.get("normalized_label")
        or ""
    )


def map_record(rec: dict) -> Optional[dict]:
    """
    Map one trace record to a compact twin event, or None if not relevant.

    Returns dict with: kind (attach|release|measurement), t (float epoch),
    ue (str id), cell (int|None), trace_msg, interface, procedure_id.
    """
    name = _label(rec)
    if not name or name in _SKIP:
        return None
    kind = None
    if name in _ATTACH:
        kind = "attach"
    elif name in _RELEASE:
        kind = "release"
    elif name in _MEASUREMENT:
        kind = "measurement"
    else:
        return None

    ue = rec.get("m_tmsi")
    if ue is None:
        ue = rec.get("procedure_id")
    if ue is None:
        return None

    return {
        "kind": kind,
        "t": parse_timestamp(rec.get("timestamp", "")),
        "ue": str(ue),
        "cell": rec.get("cell_id"),
        "trace_msg": name,
        "interface": rec.get("interface"),
        "procedure_id": rec.get("procedure_id"),
    }


def iter_trace_records(path: Path | str) -> Iterator[dict]:
    """Stream objects from a top-level JSON array without loading the full file."""
    path = Path(path)
    dec = json.JSONDecoder()
    with path.open("r", encoding="utf-8") as fp:
        buf = ""
        while "[" not in buf:
            extra = fp.read(65536)
            if not extra:
                return
            buf += extra
        pos = buf.index("[") + 1

        while True:
            buf = buf[pos:].lstrip()
            pos = 0
            if not buf:
                extra = fp.read(65536)
                if not extra:
                    return
                buf = extra
                continue
            if buf[pos] == "]":
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf, pos)
                except json.JSONDecodeError:
                    extra = fp.read(262144)
                    if not extra:
                        raise
                    buf += extra
                    continue
                yield obj
                pos = end
                while pos < len(buf) and buf[pos] in ", \r\n\t":
                    pos += 1
                if pos < len(buf) and buf[pos] == "]":
                    return
                break


def iter_trace_events(path: Path | str) -> Iterator[dict]:
    """Yield mapped twin events from one trace file."""
    for rec in iter_trace_records(path):
        ev = map_record(rec)
        if ev:
            ev["source"] = str(path)
            yield ev
