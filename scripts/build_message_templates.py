#!/usr/bin/env python3
"""
Build a {message_name: template} map from decoded call-trace JSON files.

For each distinct signalling message type seen in the traces, keep one
representative record and abstract its per-instance envelope fields into
``<<token>>`` placeholders (see common/signaling/templates.py). The result is a
compact JSON the LTE catalog loads at runtime to build realistic messages; any
message the catalog needs but the traces don't contain is covered by the catalog's
built-in DEFAULT_TEMPLATES.

  python scripts/build_message_templates.py
  python scripts/build_message_templates.py --trace-dir 22_decoded --out data/lte_templates.json
  python scripts/build_message_templates.py --max-files 5      # quick: scan a few files

Runtime wiring: LTE_TEMPLATES=data/lte_templates.json on du/ru/ue-sim (see docker-compose.yml).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.call_trace import _label, iter_trace_records  # noqa: E402
from common.signaling.templates import abstract_record  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Build signalling message templates for the twin")
    ap.add_argument("--trace-dir", type=Path, default=ROOT / "22_decoded")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "lte_templates.json")
    ap.add_argument("--max-files", type=int, default=20, help="0 = all files (slow)")
    args = ap.parse_args()

    files = sorted(args.trace_dir.rglob("*_TRC_decoded.json"))
    if args.max_files:
        files = files[: args.max_files]
    if not files:
        print(f"no trace files under {args.trace_dir}", file=sys.stderr)
        sys.exit(1)

    templates: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name} ...", flush=True)
        for rec in iter_trace_records(path):
            if rec.get("decoding_status") != "success":
                continue
            name = _label(rec)
            if not name:
                continue
            counts[name] = counts.get(name, 0) + 1
            if name not in templates:
                templates[name] = abstract_record(rec)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as out:
        json.dump(templates, out, indent=1, ensure_ascii=False)

    print(f"\ndone: {len(templates)} message templates from {len(files)} files -> {args.out}")
    for name in sorted(counts, key=lambda n: -counts[n])[:25]:
        print(f"  {counts[name]:7d}  {name}")


if __name__ == "__main__":
    main()
