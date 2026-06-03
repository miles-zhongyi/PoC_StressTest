"""
Catalog of record_id / message_name pairs from CallFlow/record-id-messages.txt
and representative sample lookup in the 22_decoded TRC JSON files.

Samples are matched by **message_name** (the stable per-type identifier), not by
record_id — record_id is only a position-ish id inside each trace file and the same
message type appears under different record_ids, so requiring record_id equality
left many real messages "unindexed" even though they exist.

Important: indexing scans large trace files, so it runs **once** in a background
thread (bounded by TRACE_SAMPLE_MAX_FILES) and is cached to disk. Request handlers
NEVER scan — get_sample() only reads the in-memory, name-keyed index. This keeps the
dashboard responsive (a previous synchronous full-tree scan per request hung it).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from common.call_trace import _label, iter_trace_records
from common.signaling.templates import TEMPLATE_TOKENS, abstract_record

_LINE = re.compile(r"^record_id\s+(\d+)\s*:\s*(.+)$", re.IGNORECASE)
# Cap how many trace files the background indexer reads. Flow message types all
# appear in the first handful of files; this bounds the one-time scan cost.
MAX_FILES = int(os.environ.get("TRACE_SAMPLE_MAX_FILES", "60"))
_CACHE_VERSION = 4  # bump when the cache layout changes (invalidates old caches)


def parse_record_id_messages(path: Path) -> list[dict]:
    """Parse record-id-messages.txt into selectable catalog entries."""
    text = path.read_text(encoding="utf-8")
    section = "OTHER"
    entries: list[dict] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not _LINE.match(line):
            section = line.lstrip("#").strip().split()[0] or section
            continue
        m = _LINE.match(line)
        if not m:
            continue
        record_id = int(m.group(1))
        for message_name in _split_message_names(m.group(2)):
            key = f"{record_id}:{message_name}"
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "key": key,
                "record_id": record_id,
                "message_name": message_name,
                "section": section,
                "label": f"{section} · record {record_id} · {message_name}",
            })
    return entries


def _split_message_names(blob: str) -> list[str]:
    names: list[str] = []
    for pipe_part in re.split(r"\s*\|\s*", blob):
        for part in pipe_part.split(","):
            name = part.strip()
            if name:
                names.append(name)
    return names


def _sample_payload(rid: int, rec: dict, path: Path) -> dict:
    """Trace record plus the twin template derived from it (same as build_message_templates)."""
    return {
        "key": str(rid),
        "record_id": rid,
        "message_name": _label(rec) or rec.get("message_name"),
        "source_file": path.name,
        "source_path": str(path),
        "message": rec,
        "template": abstract_record(rec),
        "template_tokens": list(TEMPLATE_TOKENS),
    }


class TraceCatalog:
    def __init__(self, catalog_path: Path, trace_dir: Path, cache_path: Path | None = None):
        self.catalog_path = catalog_path
        self.trace_dir = trace_dir
        self.cache_path = cache_path
        self.entries = parse_record_id_messages(catalog_path)
        # record_id is the wire identifier the twin stamps (record-id-messages.txt); a
        # message_name index alongside lets the fidelity check match same-type records
        # even where a record_id collides with another decoded type.
        self.wanted_ids = sorted({e["record_id"] for e in self.entries})
        self.wanted_names = sorted({e["message_name"] for e in self.entries})
        self._lock = threading.Lock()
        self._by_record: dict[int, dict] = {}     # record_id    -> sample payload
        self._by_name: dict[str, dict] = {}       # message_name -> sample payload
        self._status = {
            "ready": False,
            "building": False,
            "scanned_done": False,   # a full bounded scan has completed at least once
            "found": 0,              # distinct record_ids sampled
            "total": len(self.wanted_ids),
            "entries_found": 0,      # catalog entries resolvable (by record_id)
            "entries_total": len(self.entries),
            "files_scanned": 0,
            "error": None,
            "cache_loaded": False,
        }
        if cache_path and cache_path.is_file():
            self._load_cache(cache_path)

    # ---- public API (called by the HTTP handler — must never scan) -------
    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def list_entries(self) -> list[dict]:
        with self._lock:
            ids = set(self._by_record)
            return [{**e, "found": e["record_id"] in ids} for e in self.entries]

    def get_by_record_id(self, rid) -> dict | None:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            return None
        with self._lock:
            hit = self._by_record.get(rid)
        return dict(hit) if hit else None

    def get_sample(self, key: str) -> dict | None:
        """Resolve a sample by record_id (preferred), a 'record_id:name' catalog key, or
        a bare message name. In-memory only — never scans (keeps request threads fast)."""
        key = str(key).strip()
        if key.isdigit():
            return self.get_by_record_id(int(key))
        if ":" in key and key.split(":", 1)[0].isdigit():
            return self.get_by_record_id(int(key.split(":", 1)[0]))
        with self._lock:                          # bare message name
            hit = self._by_name.get(key)
        return dict(hit) if hit else None

    def ensure_index(self):
        """Kick off the one-time background scan if it hasn't run yet."""
        with self._lock:
            if self._status["scanned_done"] or self._status["building"]:
                return
            self._status["building"] = True
        threading.Thread(target=self._build_index, daemon=True).start()

    # ---- background indexing --------------------------------------------
    def _build_index(self):
        try:
            if not self.trace_dir.is_dir():
                raise FileNotFoundError(f"trace dir not found: {self.trace_dir}")
            wanted_ids = set(self.wanted_ids)
            wanted_names = set(self.wanted_names)
            files = sorted(self.trace_dir.rglob("*_TRC_decoded.json"))[:MAX_FILES]
            files_scanned = 0
            for path in files:
                with self._lock:
                    rem_ids = wanted_ids - set(self._by_record)
                    rem_names = wanted_names - set(self._by_name)
                if not rem_ids and not rem_names:
                    break
                files_scanned += 1
                for rec in iter_trace_records(path):
                    if rec.get("decoding_status") not in (None, "success"):
                        continue
                    rid = rec.get("record_id")
                    label = _label(rec)
                    if rid in rem_ids or label in rem_names:
                        self._store(rid, label, rec, path, rem_ids, rem_names)
                    if not rem_ids and not rem_names:
                        break
            with self._lock:
                self._status.update(
                    files_scanned=files_scanned,
                    found=len(self._by_record),
                    entries_found=sum(1 for e in self.entries if e["record_id"] in self._by_record),
                    ready=True, building=False, scanned_done=True,
                )
            self._save_cache()
        except Exception as exc:
            with self._lock:
                self._status.update(error=str(exc), building=False, ready=True, scanned_done=True)

    def _store(self, rid, label, rec, path, rem_ids, rem_names):
        payload = _sample_payload(rid, rec, path)
        with self._lock:
            if rid in rem_ids and rid not in self._by_record:
                self._by_record[rid] = payload
                rem_ids.discard(rid)
            if label in rem_names and label not in self._by_name:
                self._by_name[label] = payload
                rem_names.discard(label)

    # ---- cache ----------------------------------------------------------
    def _load_cache(self, path: Path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if data.get("version") != _CACHE_VERSION or not isinstance(data.get("by_record"), dict):
            return  # stale/old-format cache -> ignore, will rebuild
        with self._lock:
            # JSON object keys are strings -> coerce back to int record_ids
            self._by_record.update({int(k): v for k, v in data["by_record"].items()})
            self._by_name.update(data.get("by_name") or {})
            self._status.update(
                found=len(self._by_record),
                entries_found=sum(1 for e in self.entries if e["record_id"] in self._by_record),
                files_scanned=int(data.get("files_scanned", 0)),
                ready=True, cache_loaded=True, scanned_done=True,
            )

    def _save_cache(self):
        if not self.cache_path:
            return
        with self._lock:
            payload = {
                "version": _CACHE_VERSION,
                "files_scanned": self._status["files_scanned"],
                "by_record": {str(k): v for k, v in self._by_record.items()},
                "by_name": dict(self._by_name),
            }
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
