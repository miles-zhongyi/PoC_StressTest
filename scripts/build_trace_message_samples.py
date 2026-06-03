#!/usr/bin/env python3
"""Pre-index one sample per record-id-messages.txt entry into data/trace_message_samples.json."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Repo root must be on sys.path so `common` and `dashboard` both resolve.
sys.path.insert(0, str(ROOT))

from dashboard.trace_catalog import TraceCatalog  # noqa: E402


def main():
    catalog = ROOT / "CallFlow" / "record-id-messages.txt"
    traces = ROOT / "22_decoded"
    out = ROOT / "data" / "trace_message_samples.json"
    cat = TraceCatalog(catalog, traces, out)
    print(f"indexing {cat.status()['total']} entries from {traces} ...", flush=True)
    cat._build_index()
    st = cat.status()
    print(f"done: {st['found']}/{st['total']} samples, {st['files_scanned']} files -> {out}")


if __name__ == "__main__":
    main()
