"""
UE Simulator — one process, thousands of UEs.
=============================================

This replaces the "one container per UE" approach, which Docker cannot sustain at
the thousands needed for a real stress test (per-container memory, namespaces,
daemon bookkeeping, and host networking all fall over well before then).

Instead, a single simulator process runs NUM_UES UEs concurrently as asyncio
tasks. Each UE is an independent logical entity with its own identity, mobility,
power and traffic demand, and its own socket to the serving RU. A few thousand
async tasks + sockets cost tens of MB, not tens of GB.

Why a SEPARATE simulator rather than embedding UEs in the RU container:
the UEs are decoupled from any RU, so a UE can later hand over to a different RU
just by redirecting its connection — no cross-container state migration. Embedding
UEs inside an RU would bind each UE to that RU's process and make handover hard.

Scale further horizontally by running several simulator replicas across cores:
    docker compose up --scale ue-sim=4

Runtime load control (dashboard or curl):
    curl -X POST http://localhost:8081/control -d '{"num_ues": 500}'
"""
import asyncio
import json
import math
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_p, "common")):
        sys.path.insert(0, _p)
        break

from common import protocol as P
from common import rf_model as rf
from common.signaling import get_catalog, twin
from common.signaling import procedures as proc
from pathlib import Path

from common.trace_replay import TraceReplayPlan, group_by_ue, load_index, select_ues

# Radio technology catalog (LTE today; RADIO_TECH=nr reserved for 5G). Turns the
# logical flow steps into realistic signalling messages on the wire.
CATALOG = get_catalog()


def _is_reject(reply) -> bool:
    return twin(reply).get("logical") == proc.RRC_CONNECTION_REJECT

# ---- configuration (env-overridable) ------------------------------------
RU_HOST = os.environ.get("RU_HOST", "127.0.0.1")
RU_PORT = int(os.environ.get("RU_PORT", "38470"))
NUM_UES = int(os.environ.get("NUM_UES", "1"))
MAX_UES = int(os.environ.get("MAX_UES", "5000"))
ID_PREFIX = os.environ.get("UE_PREFIX") or f"ue-{socket.gethostname()}"

# ---- RU cluster + handover ----------------------------------------------
# RU defaults mirror ru_server.py so the UE can estimate per-RU RSRP locally.
DEFAULT_TX_POWER_DBM = float(os.environ.get("RU_TX_POWER_DBM", "49"))
DEFAULT_FREQ_GHZ = float(os.environ.get("RU_FREQ_GHZ", "3.5"))
DEFAULT_TX_GAIN_DB = float(os.environ.get("RU_TX_GAIN_DB", "15"))
# A3-style hysteresis: only hand over when a neighbour beats the serving RU by
# this margin (dB). Prevents ping-pong at the cell boundary.
HO_MARGIN_DB = float(os.environ.get("HO_MARGIN_DB", "3"))


def _parse_rus():
    """Build the RU cluster the UEs can attach to.

    RU_LIST is a JSON array of {name, host, port, x, y, [tx_power_dbm, freq_ghz,
    tx_gain_db]}. Falls back to a single RU at the origin from RU_HOST/RU_PORT so
    the old single-cell setup keeps working unchanged.
    """
    raw = os.environ.get("RU_LIST", "").strip()
    items = json.loads(raw) if raw else [
        {"name": os.environ.get("CELL_ID", "cell-1"), "host": RU_HOST, "port": RU_PORT, "x": 0.0, "y": 0.0}
    ]
    rus = []
    for it in items:
        rus.append({
            "name": it.get("name", f"{it['host']}:{it['port']}"),
            "host": it["host"],
            "port": int(it["port"]),
            "x": float(it.get("x", 0.0)),
            "y": float(it.get("y", 0.0)),
            "tx_power_dbm": float(it.get("tx_power_dbm", DEFAULT_TX_POWER_DBM)),
            "freq_ghz": float(it.get("freq_ghz", DEFAULT_FREQ_GHZ)),
            "tx_gain_db": float(it.get("tx_gain_db", DEFAULT_TX_GAIN_DB)),
        })
    return rus


RUS = _parse_rus()


def rsrp_from(ru, pos):
    """Estimated RSRP (dBm) the UE would see from `ru` at `pos` — same path-loss
    model the RU itself uses, so the UE's handover decision matches RU reality."""
    d = max(1.0, math.hypot(pos["x"] - ru["x"], pos["y"] - ru["y"]))
    return rf.rsrp_dbm(ru["tx_power_dbm"], d, ru["freq_ghz"], ru["tx_gain_db"])


def best_ru(pos):
    """The strongest RU (highest RSRP) for a given position."""
    return max(RUS, key=lambda r: rsrp_from(r, pos))

# Traffic profile: voip (default) uses kbps-scale voice demand; data restores Mbps stress.
TRAFFIC_PROFILE = os.environ.get("TRAFFIC_PROFILE", "voip").lower()
# per-UE heterogeneity ("different power", demand, speed)
_default_demand = ("0.012", "0.048") if TRAFFIC_PROFILE != "data" else ("5", "30")
DEMAND_MIN = float(os.environ.get("DEMAND_MIN_MBPS", _default_demand[0]))
DEMAND_MAX = float(os.environ.get("DEMAND_MAX_MBPS", _default_demand[1]))
TX_POWERS = [float(x) for x in os.environ.get("UE_TX_POWERS_DBM", "20,23,26").split(",")]
SPEED_MIN = float(os.environ.get("SPEED_MIN_MPS", "1"))
SPEED_MAX = float(os.environ.get("SPEED_MAX_MPS", "30"))
MAX_RADIUS_M = float(os.environ.get("MAX_RADIUS_M", "1100"))
START_RADIUS_M = float(os.environ.get("START_RADIUS_M", "300"))

REPORT_INTERVAL = float(os.environ.get("REPORT_INTERVAL", "2"))
DATA_INTERVAL = float(os.environ.get("DATA_INTERVAL", "1"))
SESSION_DURATION = float(os.environ.get("SESSION_DURATION", "45"))  # 0 = forever
_BANDWIDTH_HZ = float(os.environ.get("RU_BANDWIDTH_MHZ", "100")) * 1e6

# ---- live geometry for the dashboard map --------------------------------
_geo_lock = threading.Lock()
_ue_geo: dict[str, dict] = {}
GEO_STALE_SEC = float(os.environ.get("GEO_STALE_SEC", "10"))
GEO_FROZEN_SEC = float(os.environ.get("GEO_FROZEN_SEC", "18"))
GEO_EDGE_FRAC = float(os.environ.get("GEO_EDGE_FRAC", "0.92"))
GEO_MOVE_EPS_M = float(os.environ.get("GEO_MOVE_EPS_M", "3"))


def _ue_rf(serving, pos):
    d = max(1.0, math.hypot(pos["x"] - serving["x"], pos["y"] - serving["y"]))
    return {
        "rsrp_dbm": round(rsrp_from(serving, pos), 1),
        "sinr_dl_db": round(
            rf.sinr_db(serving["tx_power_dbm"], d, serving["freq_ghz"], _BANDWIDTH_HZ, serving["tx_gain_db"]),
            1,
        ),
    }


def set_ue_geo(uid, pos, *, serving=None, state="idle", txp=23.0):
    """Record UE position for GET /geo (dashboard mobility map)."""
    now = time.time()
    x, y = float(pos["x"]), float(pos["y"])
    dist = math.hypot(x, y)
    with _geo_lock:
        prev = _ue_geo.get(uid, {})
        last_move = prev.get("last_move_ts", now)
        if prev.get("x") is None or math.hypot(x - prev["x"], y - prev["y"]) >= GEO_MOVE_EPS_M:
            last_move = now
        entry = {
            "id": uid,
            "x": round(x, 1),
            "y": round(y, 1),
            "dist_m": round(dist, 1),
            "cell": serving["name"] if serving else prev.get("cell"),
            "state": state,
            "updated_ts": now,
            "last_move_ts": last_move,
        }
        if serving is not None:
            entry.update(_ue_rf(serving, pos))
        elif "rsrp_dbm" in prev:
            entry["rsrp_dbm"] = prev["rsrp_dbm"]
            entry["sinr_dl_db"] = prev.get("sinr_dl_db")
        _ue_geo[uid] = entry


def clear_ue_geo(uid):
    with _geo_lock:
        _ue_geo.pop(uid, None)


def build_geo():
    """Snapshot of RU sites and UE positions for the dashboard map."""
    now = time.time()
    cells = [{"name": r["name"], "x": r["x"], "y": r["y"]} for r in RUS]
    with _geo_lock:
        items = list(_ue_geo.values())
    ues = []
    anomalies = []
    counts = {"tracked": 0, "connected": 0, "attaching": 0, "edge": 0, "frozen": 0, "stale": 0}
    for e in items:
        counts["tracked"] += 1
        if e.get("state") == "connected":
            counts["connected"] += 1
        elif e.get("state") == "attaching":
            counts["attaching"] += 1
        row = {k: e[k] for k in ("id", "x", "y", "dist_m", "cell", "state", "rsrp_dbm", "sinr_dl_db") if k in e}
        ues.append(row)
        if e.get("state") != "connected":
            continue
        flags = []
        if e["dist_m"] >= MAX_RADIUS_M * GEO_EDGE_FRAC:
            flags.append("edge")
            counts["edge"] += 1
        if now - e.get("last_move_ts", now) >= GEO_FROZEN_SEC:
            flags.append("frozen")
            counts["frozen"] += 1
        if now - e.get("updated_ts", now) >= GEO_STALE_SEC:
            flags.append("stale")
            counts["stale"] += 1
        if flags:
            anomalies.append({"id": e["id"], "flags": flags, "x": e["x"], "y": e["y"], "cell": e.get("cell")})
    return {
        "ts": now,
        "bounds": {
            "max_radius_m": MAX_RADIUS_M,
            "start_radius_m": START_RADIUS_M,
            "coverage_hint_m": 1300,
        },
        "ho_margin_db": HO_MARGIN_DB,
        "cells": cells,
        "ues": ues,
        "anomalies": anomalies,
        "counts": counts,
    }
IDLE_BETWEEN = float(os.environ.get("IDLE_BETWEEN", "5"))
RAMP_SECONDS = float(os.environ.get("RAMP_SECONDS", "10"))          # spread attaches
STATS_INTERVAL = float(os.environ.get("STATS_INTERVAL", "5"))
HTTP_PORT = int(os.environ.get("UE_HTTP_PORT", "8081"))
def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "trace")


REPLAY_MODE = _env_flag("REPLAY_MODE", "0")
TRACE_INDEX = os.environ.get("TRACE_INDEX", "")
REPLAY_SPEED = float(os.environ.get("REPLAY_SPEED", "1.0"))
MAX_REPLAY_UES = int(os.environ.get("MAX_REPLAY_UES", "0"))  # 0 = use target_num_ues
TARGET_STATE_PATH = os.environ.get("UE_TARGET_STATE", "/trace/data/ue_target.json")


def _load_persisted_target():
    """Restore dashboard scale after ue-sim container restart."""
    if not TARGET_STATE_PATH:
        return None
    path = Path(TARGET_STATE_PATH)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        n = int(data["num_ues"])
        return max(0, min(n, MAX_UES))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _save_persisted_target(n: int):
    if not TARGET_STATE_PATH:
        return
    path = Path(TARGET_STATE_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"num_ues": n, "updated": time.time()}),
            encoding="utf-8",
        )
    except OSError as exc:
        log(f"warning: could not persist UE target to {path}: {exc}")


_persisted = _load_persisted_target()
target_num_ues = _persisted if _persisted is not None else NUM_UES
ue_tasks: dict[int, asyncio.Task] = {}
_loop: asyncio.AbstractEventLoop | None = None
_snap_json = b'{"active":0}'
_reconcile_lock = asyncio.Lock()
_scaling = False
_replaying = False
SPAWN_BATCH = int(os.environ.get("UE_SPAWN_BATCH", "25"))
SPAWN_INTERVAL = float(os.environ.get("UE_SPAWN_INTERVAL", "0.2"))


class Stats:
    def __init__(self):
        self.active = 0          # currently connected (gauge)
        self.attempts = 0
        self.admitted = 0
        self.rejected = 0
        self.released = 0
        self.dropped = 0         # lost coverage mid-session
        self.conn_err = 0        # couldn't reach RU
        self.handovers = 0       # successful inter-RU handovers
        self.ho_fail = 0         # handover attempts the target RU refused


S = Stats()


def is_verbose():
    v = os.environ.get("VERBOSE", "").lower()
    return v in ("1", "true", "yes") or target_num_ues <= 10


def running_ue_count():
    return sum(1 for t in ue_tasks.values() if not t.done())


def sim_mode_label():
    if _replaying:
        return "replaying"
    if _scaling:
        return "scaling"
    return "synthetic"


def replay_wanted() -> bool:
    """Optional trace replay at startup; always followed by synthetic load."""
    if not REPLAY_MODE:
        return False
    if _env_flag("REPLAY_SKIP", "0"):
        return False
    if not TRACE_INDEX:
        return False
    return Path(TRACE_INDEX).is_file()


def build_status():
    return {
        "num_ues_configured": target_num_ues,
        "num_ues_running": running_ue_count(),
        "scaling": _scaling,
        "sim_mode": sim_mode_label(),
        "replay_mode_env": REPLAY_MODE,
        "num_ues_max": MAX_UES,
        "ru": ",".join(r["name"] for r in RUS),
        "num_rus": len(RUS),
        "active": S.active,
        "attempts": S.attempts,
        "admitted": S.admitted,
        "rejected": S.rejected,
        "released": S.released,
        "dropped": S.dropped,
        "conn_err": S.conn_err,
        "handovers": S.handovers,
        "ho_fail": S.ho_fail,
        "ts": time.time(),
    }


def refresh_status():
    global _snap_json
    _snap_json = json.dumps(build_status()).encode()


async def reconcile():
    """Spawn or cancel UE tasks to match target_num_ues (batched to avoid RU/DU storms)."""
    global _scaling
    async with _reconcile_lock:
        _scaling = True
        refresh_status()
        try:
            while True:
                goal = target_num_ues
                for idx in sorted(list(ue_tasks.keys()), reverse=True):
                    if idx >= goal:
                        ue_tasks[idx].cancel()
                        del ue_tasks[idx]
                refresh_status()

                to_spawn = [
                    idx for idx in range(goal)
                    if ue_tasks.get(idx) is None or ue_tasks[idx].done()
                ]
                for start in range(0, len(to_spawn), SPAWN_BATCH):
                    if goal != target_num_ues:
                        break
                    for idx in to_spawn[start : start + SPAWN_BATCH]:
                        ue_tasks[idx] = asyncio.create_task(run_ue(idx))
                    refresh_status()
                    if start + SPAWN_BATCH < len(to_spawn):
                        await asyncio.sleep(SPAWN_INTERVAL)
                if goal == target_num_ues:
                    break
        finally:
            _scaling = False
            refresh_status()


def schedule_reconcile():
    """Run reconcile on the event loop without blocking the HTTP /control thread."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(reconcile(), _loop)


async def set_target(n: int) -> dict:
    global target_num_ues
    n = max(0, min(int(n), MAX_UES))
    changed = n != target_num_ues
    if changed:
        old = target_num_ues
        target_num_ues = n
        _save_persisted_target(n)
        schedule_reconcile()
        log(f"target UEs {old} -> {n} (scaling in background, batch={SPAWN_BATCH})")
    return {"ok": True, "changed": changed, **build_status()}


def start_http():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _json_response(self, code: int, payload: dict):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/geo"):
                body = json.dumps(build_geo()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path.startswith("/status"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(_snap_json)))
                self.end_headers()
                self.wfile.write(_snap_json)
                return
            s = build_status()
            body = (
                f"UE simulator: target={s['num_ues_configured']} running={s['num_ues_running']}\n"
                f"active={s['active']} admitted={s['admitted']} rejected={s['rejected']}\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.path.startswith("/control"):
                self._json_response(404, {"ok": False, "error": "not found"})
                return
            if _loop is None:
                self._json_response(503, {"ok": False, "error": "simulator not ready"})
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode() or "{}")
                n = int(body["num_ues"])
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                self._json_response(400, {"ok": False, "error": f"bad request: {exc}"})
                return
            fut = asyncio.run_coroutine_threadsafe(set_target(n), _loop)
            try:
                result = fut.result(timeout=15)
            except Exception as exc:
                self._json_response(500, {"ok": False, "error": str(exc)})
                return
            self._json_response(200, result)

    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def log(msg):
    print(f"[UE-SIM {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def vlog(uid, msg):
    if is_verbose():
        print(f"[{uid} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Walk:
    """Bounded 2-D random walk around the cluster origin (0,0). With RUs placed
    on either side of the origin, crossing the midline is what triggers handovers."""

    def __init__(self, speed):
        ang = random.uniform(0, 2 * math.pi)
        r = random.uniform(50, START_RADIUS_M)
        self.x = r * math.cos(ang)
        self.y = r * math.sin(ang)
        self.heading = random.uniform(0, 2 * math.pi)
        self.speed = speed

    def pos(self):
        return {"x": round(self.x, 1), "y": round(self.y, 1)}

    def step(self, dt):
        self.heading += random.gauss(0, 0.5)
        self.x += self.speed * dt * math.cos(self.heading)
        self.y += self.speed * dt * math.sin(self.heading)
        if math.hypot(self.x, self.y) > MAX_RADIUS_M:
            self.heading = math.atan2(-self.y, -self.x) + random.gauss(0, 0.3)
        return self.pos()


async def _attach(ru, uid, demand, txp, pos):
    """Open a Uu link to `ru` and walk the full attach call flow (RRC connection
    request -> setup -> setup complete -> security -> capability -> initial context
    setup). Returns (reader, writer, admission_reply): the reply is the network's
    Initial Context Setup Request on success or an RRC Connection Reject on failure;
    callers check `_is_reject(reply)`."""
    reader, writer = await asyncio.open_connection(ru["host"], ru["port"])
    cell = ru["name"]
    admission = None
    for step in CATALOG.attach_flow():
        # Carry geometry on every uplink so the RU can compute RF; the demand is only
        # needed where admission (the PRB grant) is decided.
        demand_mbps = demand if step.action == proc.ACT_ADMIT else None
        msg = CATALOG.build(step.uplink, ue_id=uid, cell=cell, step=step.name,
                            position=pos, tx_power_dbm=txp, demand_mbps=demand_mbps)
        await P.async_send_msg(writer, msg)
        reply = await P.async_recv_msg(reader)
        if step.action == proc.ACT_ADMIT:
            admission = reply
            if _is_reject(reply):
                return reader, writer, reply
    return reader, writer, admission


async def _release(reader, writer, uid, cell):
    """Gracefully walk the release flow so the serving cell reclaims the UE's PRBs."""
    try:
        for step in CATALOG.release_flow():
            await P.async_send_msg(writer, CATALOG.build(step.uplink, ue_id=uid, cell=cell, step=step.name))
            await P.async_recv_msg(reader)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    try:
        writer.close()
    except OSError:
        pass


async def one_session(uid, demand, txp, walk):
    """Attach to the strongest RU, communicate while moving — handing over to a
    neighbour RU when it becomes stronger by HO_MARGIN_DB — then release."""
    S.attempts += 1
    pos = walk.pos()
    serving = best_ru(pos)
    set_ue_geo(uid, pos, serving=serving, state="attaching", txp=txp)
    try:
        reader, writer, reply = await _attach(serving, uid, demand, txp, pos)
    except (OSError, asyncio.IncompleteReadError, ConnectionError):
        S.conn_err += 1
        clear_ue_geo(uid)
        return
    if _is_reject(reply):
        S.rejected += 1
        vlog(uid, f"attach rejected on {serving['name']}: {twin(reply).get('cause')}")
        try:
            writer.close()
        except OSError:
            pass
        clear_ue_geo(uid)
        return

    S.admitted += 1
    S.active += 1
    set_ue_geo(uid, pos, serving=serving, state="connected", txp=txp)
    vlog(uid, f"connected to {serving['name']}: {twin(reply).get('allocated_prbs')} PRB, "
         f"MCS {twin(reply).get('mcs')}")
    try:
        start = now = time.time()
        last_report = last_data = start
        while SESSION_DURATION == 0 or (now - start) < SESSION_DURATION:
            now = time.time()
            if now - last_report >= REPORT_INTERVAL:
                pos = walk.step(now - last_report)
                last_report = now
                set_ue_geo(uid, pos, serving=serving, state="connected", txp=txp)

                # ---- handover decision (A3-style, evaluated by the UE) ----
                target = best_ru(pos)
                if (target["name"] != serving["name"]
                        and rsrp_from(target, pos) >= rsrp_from(serving, pos) + HO_MARGIN_DB):
                    try:
                        n_reader, n_writer, n_reply = await _attach(target, uid, demand, txp, pos)
                    except (OSError, asyncio.IncompleteReadError, ConnectionError):
                        S.ho_fail += 1                      # couldn't reach target -> stay
                    else:
                        if _is_reject(n_reply):
                            S.ho_fail += 1                  # target full / no coverage -> stay
                            vlog(uid, f"HO {serving['name']}->{target['name']} rejected: {twin(n_reply).get('cause')}")
                            try:
                                n_writer.close()
                            except OSError:
                                pass
                        else:
                            # make-before-break: target admitted us, now drop the old cell
                            await _release(reader, writer, uid, serving["name"])
                            reader, writer, serving = n_reader, n_writer, target
                            S.handovers += 1
                            set_ue_geo(uid, pos, serving=serving, state="connected", txp=txp)
                            vlog(uid, f"HO -> {target['name']}: {twin(n_reply).get('allocated_prbs')} PRB")
                            continue

                # measurement report on the (possibly unchanged) serving RU
                await P.async_send_msg(writer, CATALOG.build(
                    proc.RRC_MEASUREMENT_REPORT, ue_id=uid, cell=serving["name"],
                    step=proc.STEP_MEASUREMENT, position=pos, tx_power_dbm=txp))
                r = await P.async_recv_msg(reader)
                if _is_reject(r):
                    S.dropped += 1
                    vlog(uid, f"dropped on {serving['name']}: {twin(r).get('cause')}")
                    set_ue_geo(uid, pos, serving=serving, state="dropped", txp=txp)
                    return
            if now - last_data >= DATA_INTERVAL:
                # VoIP-sized PDU (~20 ms frame); data profile keeps a larger stub.
                payload = 500 if TRAFFIC_PROFILE != "data" else 1_000_000
                await P.async_send_msg(writer, CATALOG.build(
                    proc.UE_DATA, ue_id=uid, cell=serving["name"], step=proc.STEP_DATA, bytes=payload))
                await P.async_recv_msg(reader)
                last_data = now
            await asyncio.sleep(0.2)

        set_ue_geo(uid, walk.pos(), serving=serving, state="releasing", txp=txp)
        await _release(reader, writer, uid, serving["name"])
        writer = None
        S.released += 1
        vlog(uid, "released")
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        S.dropped += 1
    except asyncio.CancelledError:
        if writer is not None:
            try:
                await _release(reader, writer, uid, serving["name"])
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
        raise
    finally:
        S.active -= 1
        clear_ue_geo(uid)
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass


async def replay_trace_ue(uid: str, plan: TraceReplayPlan, demand: float, txp: float, speed: float):
    """Drive one UE from call-trace events at the recorded times."""
    walk = Walk(speed)
    reader = writer = None
    serving = None
    connected = False
    wall0 = time.time()
    try:
        await _replay_trace_ue_body(uid, plan, demand, txp, speed, walk)
    finally:
        clear_ue_geo(uid)


async def _replay_trace_ue_body(uid, plan, demand, txp, speed, walk):
    reader = writer = None
    serving = None
    connected = False
    wall0 = time.time()
    for ev in plan.events:
        delay = plan.sim_delay(ev["t"]) - (time.time() - wall0)
        if delay > 0:
            await asyncio.sleep(delay)
        kind = ev["kind"]
        if kind == "attach" and not connected:
            S.attempts += 1
            pos = walk.pos()
            serving = best_ru(pos)
            set_ue_geo(uid, pos, serving=serving, state="attaching", txp=txp)
            try:
                reader, writer, reply = await _attach(serving, uid, demand, txp, pos)
            except (OSError, asyncio.IncompleteReadError, ConnectionError):
                S.conn_err += 1
                continue
            if _is_reject(reply):
                S.rejected += 1
                vlog(uid, f"trace attach rejected: {twin(reply).get('cause')} ({ev['trace_msg']})")
                try:
                    writer.close()
                except OSError:
                    pass
                reader = writer = None
                continue
            connected = True
            S.admitted += 1
            S.active += 1
            set_ue_geo(uid, pos, serving=serving, state="connected", txp=txp)
            vlog(uid, f"trace attach {ev['trace_msg']} -> {serving['name']} "
                 f"{twin(reply).get('allocated_prbs')} PRB")
        elif kind == "measurement" and connected and reader and writer:
            pos = walk.step(REPORT_INTERVAL)
            set_ue_geo(uid, pos, serving=serving, state="connected", txp=txp)
            await P.async_send_msg(writer, CATALOG.build(
                proc.RRC_MEASUREMENT_REPORT, ue_id=uid, cell=serving["name"],
                step=proc.STEP_MEASUREMENT, position=pos, tx_power_dbm=txp))
            r = await P.async_recv_msg(reader)
            if _is_reject(r):
                S.dropped += 1
                connected = False
                break
        elif kind == "release" and connected and reader and writer:
            await _release(reader, writer, uid, serving["name"])
            reader = writer = None
            connected = False
            S.released += 1
            S.active -= 1
            vlog(uid, f"trace release {ev['trace_msg']}")
    if connected and reader and writer:
        await _release(reader, writer, uid, serving["name"])
        S.released += 1
        S.active -= 1


async def run_replay():
    """Replay selected UEs from TRACE_INDEX at trace timestamps."""
    global _replaying
    _replaying = True
    refresh_status()
    try:
        path = Path(TRACE_INDEX)
        if not path.is_file():
            log(f"TRACE_INDEX not found: {path} — run scripts/build_trace_index.py")
            return
        log(f"loading trace index {path} ...")
        events = load_index(path)
        if not events:
            log("trace index empty")
            return
        by_ue = group_by_ue(events)
        n_pick = min(
            target_num_ues if target_num_ues > 0 else len(by_ue),
            MAX_REPLAY_UES or len(by_ue),
            len(by_ue),
        )
        picked = select_ues(by_ue, n_pick)
        log(f"replaying {len(picked)} UEs from {len(events)} events "
            f"({len(by_ue)} distinct UEs in index) speed={REPLAY_SPEED}x")
        t0 = min(e["t"] for e in events)
        tasks = []
        for ue_key, evs in picked.items():
            uid = f"{ID_PREFIX}-trace-{ue_key}"
            demand = random.uniform(DEMAND_MIN, DEMAND_MAX)
            txp = random.choice(TX_POWERS)
            speed = random.uniform(SPEED_MIN, SPEED_MAX)
            plan = TraceReplayPlan(evs, speed=REPLAY_SPEED, t0=t0)
            tasks.append(replay_trace_ue(uid, plan, demand, txp, speed))
        await asyncio.gather(*tasks)
        log("trace replay finished")
    finally:
        _replaying = False
        refresh_status()


async def run_ue(idx):
    uid = f"{ID_PREFIX}-{idx:05d}"
    demand = random.uniform(DEMAND_MIN, DEMAND_MAX)
    txp = random.choice(TX_POWERS)
    speed = random.uniform(SPEED_MIN, SPEED_MAX)
    ramp_target = max(1, target_num_ues)
    await asyncio.sleep(RAMP_SECONDS * idx / ramp_target)
    try:
        while True:
            walk = Walk(speed)
            await one_session(uid, demand, txp, walk)
            if SESSION_DURATION == 0:
                break
            await asyncio.sleep(IDLE_BETWEEN * (1 + random.random()))
    except asyncio.CancelledError:
        vlog(uid, "stopped (scale-down)")
        raise
    finally:
        clear_ue_geo(uid)


async def stats_monitor():
    last_log = 0.0
    while True:
        await asyncio.sleep(1)
        refresh_status()
        now = time.time()
        if now - last_log >= STATS_INTERVAL:
            last_log = now
            log(f"target={target_num_ues:<5} running={running_ue_count():<5} "
                f"active={S.active:<6} admitted={S.admitted:<7} rejected={S.rejected:<7} "
                f"released={S.released:<7} dropped={S.dropped:<6} "
                f"handovers={S.handovers:<6} ho_fail={S.ho_fail:<5} conn_err={S.conn_err}")


async def main():
    global _loop
    _loop = asyncio.get_running_loop()
    refresh_status()
    start_http()
    log(f"status http://0.0.0.0:{HTTP_PORT}/status  geo GET /geo  control POST /control")
    cluster = ", ".join(f"{r['name']}@({r['x']:.0f},{r['y']:.0f}) {r['host']}:{r['port']}" for r in RUS)
    log(f"RU cluster ({len(RUS)}): {cluster}  | HO margin {HO_MARGIN_DB} dB")
    asyncio.create_task(stats_monitor())

    if replay_wanted():
        log(f"REPLAY_MODE: index={TRACE_INDEX} speed={REPLAY_SPEED}x "
            f"(then synthetic target={target_num_ues})")
        await run_replay()
        log(f"continuing in synthetic mode — target {target_num_ues} UEs")
    elif REPLAY_MODE and TRACE_INDEX:
        log(f"REPLAY_MODE set but index missing at {TRACE_INDEX} — synthetic only")

    if _persisted is not None and _persisted != NUM_UES:
        log(f"restored persisted target={_persisted} (NUM_UES env={NUM_UES})")
    log(f"starting {target_num_ues} synthetic UEs (max {MAX_UES}) "
        f"(profile {TRAFFIC_PROFILE}, demand {DEMAND_MIN}-{DEMAND_MAX} Mbps, TX {TX_POWERS} dBm, "
        f"speed {SPEED_MIN}-{SPEED_MAX} m/s, ramp {RAMP_SECONDS}s, batch={SPAWN_BATCH})")
    schedule_reconcile()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("powering off")
