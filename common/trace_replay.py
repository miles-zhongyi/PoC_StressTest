"""
Time-ordered replay of call-trace events against the digital twin.

Build the index first:
  python scripts/build_trace_index.py --trace-dir 22_decoded --out data/trace_index.jsonl
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, List, Optional


def load_index(path: Path | str) -> List[dict]:
    """Load compact JSONL index (fits in RAM; filter at build time keeps size down)."""
    path = Path(path)
    events = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    events.sort(key=lambda e: (e["t"], e.get("ue", "")))
    return events


def iter_index(path: Path | str) -> Iterator[dict]:
    """Stream index lines without loading everything (for very large indexes)."""
    with Path(path).open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if line:
                yield json.loads(line)


def group_by_ue(events: List[dict]) -> dict[str, List[dict]]:
    by_ue: dict[str, List[dict]] = {}
    for ev in events:
        by_ue.setdefault(ev["ue"], []).append(ev)
    for ue in by_ue:
        by_ue[ue].sort(key=lambda e: e["t"])
    return by_ue


def select_ues(by_ue: dict[str, List[dict]], max_ues: int) -> dict[str, List[dict]]:
    """Pick UEs with a full attach→…→release arc when possible."""
    scored = []
    for ue, evs in by_ue.items():
        kinds = {e["kind"] for e in evs}
        score = (2 if "attach" in kinds else 0) + (1 if "release" in kinds else 0) + len(evs)
        scored.append((score, ue))
    scored.sort(reverse=True)
    pick = [ue for _, ue in scored[: max_ues or len(scored)]]
    return {ue: by_ue[ue] for ue in pick}


class TraceReplayPlan:
    """Per-UE timeline with simulation clock offsets."""

    def __init__(self, events: List[dict], speed: float = 1.0, t0: Optional[float] = None):
        self.events = sorted(events, key=lambda e: e["t"])
        self.speed = max(0.001, speed)
        self.t0 = t0 if t0 is not None else (self.events[0]["t"] if self.events else 0.0)

    def sim_delay(self, trace_time: float) -> float:
        """Seconds to wait in simulation before this trace instant."""
        return max(0.0, (trace_time - self.t0) / self.speed)
