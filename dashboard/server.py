"""Aggregates DU + UE simulator metrics for the web dashboard."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DU_URL = os.environ.get("DU_STATUS_URL", "http://du:8080/status")
UE_URL = os.environ.get("UE_STATUS_URL", "http://ue-sim:8081/status")
UE_CONTROL_URL = os.environ.get("UE_CONTROL_URL", "http://ue-sim:8081/control")
PORT = int(os.environ.get("DASHBOARD_PORT", "9090"))
POLL_SEC = float(os.environ.get("POLL_INTERVAL", "1"))
STATIC = Path(__file__).resolve().parent / "static"

_cache = {"ts": 0, "du": None, "ue": None, "ok": False, "error": None}
_lock = threading.Lock()


def _fetch(url):
    with urlopen(url, timeout=3) as resp:
        return json.loads(resp.read().decode())


def _poll_loop():
    while True:
        du, ue, err = None, None, None
        try:
            du = _fetch(DU_URL)
            ue = _fetch(UE_URL)
            ok = True
        except (URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            ok = False
            err = str(exc)
        with _lock:
            _cache.update(ts=time.time(), du=du, ue=ue, ok=ok, error=err)
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
            with urlopen(req, timeout=60) as resp:
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
    print(f"[dashboard] http://0.0.0.0:{PORT}/  (poll {DU_URL}, {UE_URL})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
