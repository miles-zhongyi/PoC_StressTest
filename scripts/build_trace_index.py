#!/usr/bin/env python3
"""
Build a compact JSONL index from decoded call-trace JSON files.

  python scripts/build_trace_index.py
  python scripts/build_trace_index.py --trace-dir 22_decoded --out data/trace_index.jsonl
  python scripts/build_trace_index.py --max-files 10   # quick test

Replay in the twin:
  set REPLAY_MODE=1 and TRACE_INDEX=data/trace_index.jsonl on ue-sim (see docker-compose.yml)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.call_trace import iter_trace_events  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Build call-trace JSONL index for twin replay")
    ap.add_argument("--trace-dir", type=Path, default=ROOT / "22_decoded")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "trace_index.jsonl")
    ap.add_argument("--max-files", type=int, default=0, help="0 = all files")
    args = ap.parse_args()

    files = sorted(args.trace_dir.rglob("*_TRC_decoded.json"))
    if args.max_files:
        files = files[: args.max_files]
    if not files:
        print(f"no trace files under {args.trace_dir}", file=sys.stderr)
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_rec = n_ev = 0
    with args.out.open("w", encoding="utf-8") as out:
        for i, path in enumerate(files, 1):
            print(f"[{i}/{len(files)}] {path.name} ...", flush=True)
            for ev in iter_trace_events(path):
                out.write(json.dumps(ev, separators=(",", ":")) + "\n")
                n_ev += 1
            n_rec += 1

    print(f"done: {n_ev} events from {n_rec} files → {args.out}")


if __name__ == "__main__":
    main()
