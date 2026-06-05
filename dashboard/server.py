"""Aggregates DU + UE simulator metrics for the web dashboard."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Make the `common` package importable (container copies it to /app/common; locally
# it lives one dir up) so we can describe the implemented signalling call flow.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_p, "common")):
        sys.path.insert(0, _p)
        break

try:
    from common.signaling import get_catalog
    CALLFLOW = get_catalog().describe()
except Exception as exc:  # never let the call-flow view break the metrics dashboard
    CALLFLOW = {"error": f"signalling catalog unavailable: {exc}"}

DU_URL = os.environ.get("DU_STATUS_URL", "http://du:8080/status")
DU_TRACE_URL = os.environ.get("DU_TRACE_URL", "http://du:8080/trace")
UE_URL = os.environ.get("UE_STATUS_URL", "http://ue-sim:8081/status")
UE_GEO_URL = os.environ.get("UE_GEO_URL", "http://ue-sim:8081/geo")
UE_CONTROL_URL = os.environ.get("UE_CONTROL_URL", "http://ue-sim:8081/control")
RU_STATE_DIR = Path(os.environ.get("RU_STATE_DIR", "/trace/data/ru_state"))
RU_DICTIONARY_URLS = [
    u.strip() for u in os.environ.get(
        "RU_DICTIONARY_URLS",
        "http://ru:8082/dictionary,http://ru2:8082/dictionary,http://ru3:8082/dictionary",
    ).split(",") if u.strip()
]
PORT = int(os.environ.get("DASHBOARD_PORT", "9090"))
POLL_SEC = float(os.environ.get("POLL_INTERVAL", "1"))
STATIC = Path(__file__).resolve().parent / "static"
_APP = Path(__file__).resolve().parent
_REPO_ROOT = _APP.parent if (_APP.parent / "common").is_dir() else _APP
_CATALOG_PATH = Path(os.environ.get(
    "RECORD_ID_MESSAGES",
    _REPO_ROOT / "CallFlow" / "record-id-messages.txt",
))
_TRACE_DIR = Path(os.environ.get("TRACE_DECODED_DIR", _REPO_ROOT / "22_decoded"))
_SAMPLES_CACHE = Path(os.environ.get(
    "TRACE_SAMPLES_CACHE",
    _REPO_ROOT / "data" / "trace_message_samples.json",
))

_trace_catalog = None
_trace_catalog_err = None

try:
    from common.signaling.templates import TEMPLATE_TOKENS, abstract_record
except ImportError:
    TEMPLATE_TOKENS = ()
    abstract_record = None

try:
    from trace_catalog import TraceCatalog

    if _CATALOG_PATH.is_file():
        _trace_catalog = TraceCatalog(_CATALOG_PATH, _TRACE_DIR, _SAMPLES_CACHE)
        _trace_catalog.ensure_index()
    else:
        _trace_catalog_err = f"catalog file not found: {_CATALOG_PATH}"
except Exception as exc:
    _trace_catalog_err = str(exc)

_cache = {
    "ts": 0, "du": None, "ue": None, "geo": None, "ok": False, "error": None,
    "du_fresh": False, "ue_fresh": False, "geo_fresh": False,
    "trace_ue_id": None, "trace_complete": False, "trace_fresh": False,
}
_lock = threading.Lock()


def _fetch(url, timeout=5):
    with urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _enrich_live_trace(data: dict) -> dict:
    """Add twin template per event (same abstract_record as lte_templates build)."""
    if not data or abstract_record is None:
        return data
    events = []
    for ev in data.get("events") or []:
        row = dict(ev)
        msg = row.get("message")
        if isinstance(msg, dict):
            row["template"] = abstract_record(msg)
        events.append(row)
    out = dict(data)
    out["events"] = events
    out["template_tokens"] = list(TEMPLATE_TOKENS)
    return out


def _load_ru_dict_from_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("site_id"):
            data["_source"] = "file"
            data["_path"] = str(path.name)
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def fetch_ru_dictionaries(site_filter: str | None = None) -> dict:
    """Live RU dictionaries from HTTP (preferred) with file fallback."""
    by_site: dict[str, dict] = {}
    errors: list[str] = []

    for url in RU_DICTIONARY_URLS:
        try:
            doc = _fetch(url, timeout=3)
            sid = doc.get("site_id")
            if sid:
                doc["_source"] = "live"
                doc["_url"] = url
                by_site[sid] = doc
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")

    if RU_STATE_DIR.is_dir():
        for path in sorted(RU_STATE_DIR.glob("*.json")):
            if path.name.endswith(".example.json"):
                continue
            doc = _load_ru_dict_from_file(path)
            if doc and doc["site_id"] not in by_site:
                by_site[doc["site_id"]] = doc

    sites = sorted(by_site.values(), key=lambda d: d.get("site_id", ""))
    if site_filter:
        sites = [s for s in sites if s.get("site_id") == site_filter]
    return {"ts": time.time(), "sites": sites, "errors": errors}


def _poll_loop():
    while True:
        now = time.time()
        du, ue, geo = None, None, None
        trace_ue_id, trace_complete = None, False
        du_fresh = ue_fresh = geo_fresh = trace_fresh = False
        errors = []
        try:
            du = _fetch(DU_URL)
            du_fresh = True
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"DU: {exc}")
        try:
            ue = _fetch(UE_URL)
            ue_fresh = True
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"UE: {exc}")
        try:
            geo = _fetch(UE_GEO_URL)
            geo_fresh = True
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"GEO: {exc}")
        try:
            trace = _fetch(DU_TRACE_URL)
            trace_ue_id = trace.get("ue_id")
            trace_complete = bool(trace.get("complete"))
            trace_fresh = True
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"TRACE: {exc}")
        with _lock:
            prev = dict(_cache)
            if not du_fresh:
                du = prev.get("du")
            if not ue_fresh:
                ue = prev.get("ue")
            if not geo_fresh:
                geo = prev.get("geo")
            if not trace_fresh:
                trace_ue_id = prev.get("trace_ue_id")
                trace_complete = prev.get("trace_complete", False)
            ok = du_fresh and ue_fresh and du is not None and ue is not None
            err = "; ".join(errors) if errors else None
            _cache.update(
                ts=now, du=du, ue=ue, geo=geo, ok=ok, error=err,
                du_fresh=du_fresh, ue_fresh=ue_fresh, geo_fresh=geo_fresh,
                trace_ue_id=trace_ue_id, trace_complete=trace_complete, trace_fresh=trace_fresh,
            )
        time.sleep(POLL_SEC)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/ues":
            self._send(404, '{"ok":false,"error":"not found"}', "application/json")
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        req = Request(
            UE_CONTROL_URL,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=30) as resp:
                out = resp.read()
                code = resp.status
        except HTTPError as exc:
            out = exc.read()
            code = exc.code
        except (URLError, OSError, TimeoutError) as exc:
            self._send(502, json.dumps({"ok": False, "error": str(exc)}), "application/json")
            return
        self._send(code, out, "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/metrics":
            with _lock:
                payload = dict(_cache)
            self._send(200, json.dumps(payload), "application/json")
            return
        if path == "/api/callflow":
            self._send(200, json.dumps(CALLFLOW), "application/json")
            return
        if path == "/api/fidelity":
            if _trace_catalog is None:
                self._send(503, json.dumps({"error": _trace_catalog_err}), "application/json")
                return
            try:
                from common.signaling import get_catalog
                from common.signaling.fidelity import build_fidelity_report

                def _real_by_name(name):
                    s = _trace_catalog.get_sample(name)   # by message_name; in-memory
                    return s.get("message") if s else None

                report = build_fidelity_report(get_catalog(), _real_by_name)
                self._send(200, json.dumps(report), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}), "application/json")
            return
        if path in ("/api/trace", "/api/trace/reset"):
            url = DU_TRACE_URL + ("/reset" if path.endswith("/reset") else "")
            try:
                data = _fetch(url)
                if not path.endswith("/reset"):
                    data = _enrich_live_trace(data)
                self._send(200, json.dumps(data), "application/json")
            except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
                self._send(502, json.dumps({"error": str(exc)}), "application/json")
            return
        if path == "/api/trace-catalog":
            if _trace_catalog is None:
                self._send(503, json.dumps({"error": _trace_catalog_err}), "application/json")
                return
            _trace_catalog.ensure_index()
            self._send(200, json.dumps({
                "entries": _trace_catalog.list_entries(),
                "status": _trace_catalog.status(),
            }), "application/json")
            return
        if path == "/api/trace-sample":
            if _trace_catalog is None:
                self._send(503, json.dumps({"error": _trace_catalog_err}), "application/json")
                return
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            key = (qs.get("record_id") or qs.get("key") or qs.get("name") or [""])[0]
            if not key:
                self._send(400, json.dumps({"error": "missing record_id/key/name"}), "application/json")
                return
            sample = _trace_catalog.get_sample(key)   # in-memory only; never scans
            if sample is None:
                self._send(404, json.dumps({
                    "error": f"no indexed 22_decoded sample for {key}",
                }), "application/json")
                return
            self._send(200, json.dumps(sample), "application/json")
            return
        if path == "/api/ru-dictionary":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            site = (qs.get("site") or [None])[0]
            try:
                payload = fetch_ru_dictionaries(site)
                self._send(200, json.dumps(payload), "application/json")
            except Exception as exc:
                self._send(500, json.dumps({"error": str(exc)}), "application/json")
            return
        if path in ("", "/"):
            path = "/index.html"
        rel = path.lstrip("/")
        if ".." in rel:
            self._send(403, "forbidden", "text/plain")
            return
        target = STATIC / rel
        if not target.is_file():
            self._send(404, "not found", "text/plain")
            return
        ctype = "text/html" if target.suffix == ".html" else "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)


def main():
    threading.Thread(target=_poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[dashboard] http://0.0.0.0:{PORT}/  (poll {DU_URL}, {UE_URL}, {UE_GEO_URL})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
