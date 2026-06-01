"""
DU (Distributed Unit) — asyncio, built to scale.
================================================

The DU owns the cell's PRB pool and does admission control. The capacity logic
is identical to before; what changed is the concurrency model:

  * It is now an asyncio server, so it can hold many connections cheaply.
  * The F1 link from each RU is a single MULTIPLEXED connection carrying every
    UE's signalling (correlated by a per-message `txn` id). This mirrors real F1
    (one DU<->RU association for all UEs) and means the DU holds ~1 connection
    per RU instead of one per UE.
  * Because asyncio is single-threaded and cooperative, the PRB pool is mutated
    only between `await` points, so no locks are needed: each handle_* call is
    atomic with respect to the others.

The HTTP status endpoint runs in a small background thread and serves a snapshot
that the event loop refreshes once a second (so the two threads never iterate the
same live structure).
"""
import asyncio
import json
import os
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

TCP_PORT = int(os.environ.get("DU_TCP_PORT", "38472"))
HTTP_PORT = int(os.environ.get("DU_HTTP_PORT", "8080"))
TOTAL_PRBS = int(os.environ.get("TOTAL_PRBS", "273"))
SCS_KHZ = int(os.environ.get("SCS_KHZ", "30"))
MONITOR_INTERVAL = float(os.environ.get("MONITOR_INTERVAL", "5"))
TRAFFIC_PROFILE = os.environ.get("TRAFFIC_PROFILE", "voip").lower()


def log(msg):
    print(f"[DU {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Cell:
    def __init__(self, cell_id, total_prbs):
        self.cell_id = cell_id
        self.total_prbs = total_prbs
        self.used_prbs = 0
        self.sessions = {}
        self.rejected_total = 0
        self.released_total = 0
        self.admitted_total = 0

    @property
    def free_prbs(self):
        return self.total_prbs - self.used_prbs


class DU:
    def __init__(self):
        self.cells = {}
        self._snap = {"cells": [], "ts": 0}
        self._snap_json = b'{"cells": []}'

    def _cell(self, cell_id):
        if cell_id not in self.cells:
            self.cells[cell_id] = Cell(cell_id, TOTAL_PRBS)
            log(f"cell {cell_id} brought up with {TOTAL_PRBS} PRBs")
        return self.cells[cell_id]

    # ---- procedure handlers (synchronous == atomic, never await) --------

    def handle_setup(self, msg):
        cell = self._cell(msg["cell_id"])
        ue_id = msg["ue_id"]
        sinr = msg["rf"]["sinr_dl_db"]
        demand = msg["demand_mbps"]
        required, per_prb, se = rf.prbs_for_traffic(demand, sinr, SCS_KHZ, TRAFFIC_PROFILE)
        if required is None:
            cell.rejected_total += 1
            return {"type": P.RRC_REJECT, "ue_id": ue_id, "cause": "no-coverage"}
        if required > cell.free_prbs:
            cell.rejected_total += 1
            return {"type": P.RRC_REJECT, "ue_id": ue_id, "cause": "insufficient-prb",
                    "free_prbs": cell.free_prbs, "required_prbs": required}
        cell.used_prbs += required
        cell.admitted_total += 1
        cell.sessions[ue_id] = {
            "ue_id": ue_id, "cell_id": cell.cell_id, "prbs": required,
            "sinr_dl_db": round(sinr, 1), "se": round(se, 2), "per_prb_mbps": per_prb,
            "demand_mbps": demand, "traffic_profile": TRAFFIC_PROFILE, "updated": time.time(),
        }
        return {"type": P.RRC_SETUP, "ue_id": ue_id, "cell_id": cell.cell_id,
                "allocated_prbs": required, "mcs": rf.mcs_from_se(se)}

    def handle_measurement(self, msg):
        cell = self._cell(msg["cell_id"])
        ue_id = msg["ue_id"]
        sess = cell.sessions.get(ue_id)
        if sess is None:
            return {"type": P.RRC_REJECT, "ue_id": ue_id, "cause": "unknown-ue"}
        sinr = msg["rf"]["sinr_dl_db"]
        profile = sess.get("traffic_profile", TRAFFIC_PROFILE)
        required, per_prb, se = rf.prbs_for_traffic(sess["demand_mbps"], sinr, SCS_KHZ, profile)
        if required is None:
            cell.used_prbs -= sess["prbs"]
            cell.released_total += 1
            del cell.sessions[ue_id]
            return {"type": P.RRC_REJECT, "ue_id": ue_id, "cause": "lost-coverage"}
        old = sess["prbs"]
        delta = required - old
        if delta > 0:
            grant = min(delta, cell.free_prbs)
            new_prbs = old + grant
            congested = grant < delta
        else:
            new_prbs = required
            congested = False
        if profile == "voip":
            new_prbs = min(new_prbs, rf.VOIP_MAX_PRBS)
        cell.used_prbs += (new_prbs - old)
        sess.update(prbs=new_prbs, sinr_dl_db=round(sinr, 1), se=round(se, 2),
                    per_prb_mbps=per_prb, updated=time.time(), congested=congested)
        return {"type": P.RRC_RECONFIG, "ue_id": ue_id, "allocated_prbs": new_prbs,
                "mcs": rf.mcs_from_se(se), "congested": congested}

    def handle_data(self, msg):
        cell = self._cell(msg["cell_id"])
        sess = cell.sessions.get(msg["ue_id"])
        if sess is None:
            return {"type": P.RRC_REJECT, "ue_id": msg["ue_id"], "cause": "unknown-ue"}
        return {"type": P.DATA_ACK, "ue_id": msg["ue_id"],
                "achievable_mbps": round(sess["prbs"] * sess["per_prb_mbps"], 2),
                "demand_mbps": sess["demand_mbps"]}

    def handle_release(self, msg, reason="release"):
        cell = self._cell(msg["cell_id"])
        ue_id = msg["ue_id"]
        sess = cell.sessions.pop(ue_id, None)
        if sess:
            cell.used_prbs -= sess["prbs"]
            cell.released_total += 1
        return {"type": P.RRC_RELEASE_COMPLETE, "ue_id": ue_id}

    def dispatch(self, msg):
        t = msg.get("type")
        if t == P.RRC_SETUP_REQUEST:
            return self.handle_setup(msg)
        if t == P.MEASUREMENT_REPORT:
            return self.handle_measurement(msg)
        if t == P.DATA:
            return self.handle_data(msg)
        if t == P.RRC_RELEASE:
            return self.handle_release(msg)
        return {"type": P.RRC_REJECT, "ue_id": msg.get("ue_id"), "cause": f"bad-type:{t}"}

    # ---- F1 connection (one per RU, multiplexed over all its UEs) --------

    async def serve_f1(self, reader, writer):
        peer = writer.get_extra_info("peername")
        conn_cells = set()
        log(f"F1 link up from RU {peer}")
        try:
            while True:
                msg = await P.async_recv_msg(reader)
                if msg.get("cell_id"):
                    conn_cells.add(msg["cell_id"])
                reply = self.dispatch(msg)          # atomic: no await inside
                reply["txn"] = msg.get("txn")
                await P.async_send_msg(writer, reply)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            # the RU went away -> every UE it served is gone, reclaim their PRBs
            freed = 0
            for cid in conn_cells:
                cell = self.cells.get(cid)
                if not cell:
                    continue
                for uid in list(cell.sessions):
                    freed += cell.sessions[uid]["prbs"]
                    self.handle_release({"ue_id": uid, "cell_id": cid}, "f1-down")
            if freed:
                log(f"F1 link {peer} down: reclaimed {freed} PRBs")
            writer.close()

    # ---- observability --------------------------------------------------

    def build_snapshot(self):
        cells = []
        for c in self.cells.values():
            cells.append({
                "cell_id": c.cell_id, "total_prbs": c.total_prbs,
                "used_prbs": c.used_prbs, "free_prbs": c.free_prbs,
                "utilization": round(c.used_prbs / c.total_prbs, 3) if c.total_prbs else 0,
                "connected_ues": len(c.sessions),
                "admitted_total": c.admitted_total,
                "rejected_total": c.rejected_total,
                "released_total": c.released_total,
            })
        return {"cells": cells, "ts": time.time()}

    async def monitor(self):
        last_print = 0.0
        while True:
            await asyncio.sleep(1)
            snap = self.build_snapshot()
            self._snap = snap
            self._snap_json = json.dumps(snap, indent=2).encode()
            now = time.time()
            if now - last_print >= MONITOR_INTERVAL:
                last_print = now
                for c in snap["cells"]:
                    filled = int(c["utilization"] * 30)
                    bar = "#" * filled + "-" * (30 - filled)
                    log(f"[{c['cell_id']}] PRB [{bar}] "
                        f"{c['used_prbs']}/{c['total_prbs']} ({c['utilization']*100:5.1f}%) | "
                        f"UEs={c['connected_ues']} admit={c['admitted_total']} "
                        f"reject={c['rejected_total']} released={c['released_total']}")


def start_http(du):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path.startswith("/status"):
                body = du._snap_json
                ctype = "application/json"
            else:
                lines = ["5G RU Digital Twin - DU status", ""]
                for c in du._snap["cells"]:
                    lines.append(f"cell {c['cell_id']}: {c['used_prbs']}/{c['total_prbs']} PRB "
                                 f"({c['utilization']*100:.1f}%), {c['connected_ues']} UEs, "
                                 f"admit={c['admitted_total']} reject={c['rejected_total']}")
                body = ("\n".join(lines) + "\n").encode()
                ctype = "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def main():
    du = DU()
    start_http(du)
    server = await asyncio.start_server(du.serve_f1, "0.0.0.0", TCP_PORT, backlog=512)
    asyncio.create_task(du.monitor())
    log(f"listening on :{TCP_PORT} (F1)  status http://0.0.0.0:{HTTP_PORT}/status")
    log(f"cell capacity per RU: {TOTAL_PRBS} PRBs @ {SCS_KHZ} kHz SCS  traffic={TRAFFIC_PROFILE}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down")
