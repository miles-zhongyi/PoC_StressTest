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
from common.signaling import get_catalog, twin
from common.signaling import procedures as proc

CATALOG = get_catalog()

TCP_PORT = int(os.environ.get("DU_TCP_PORT", "38472"))
HTTP_PORT = int(os.environ.get("DU_HTTP_PORT", "8080"))
TOTAL_PRBS = int(os.environ.get("TOTAL_PRBS", "273"))
SCS_KHZ = int(os.environ.get("SCS_KHZ", "30"))
TRACE_MAX = int(os.environ.get("TRACE_MAX_EVENTS", "200"))  # call-flow trace ring size (one UE)
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


_EMPTY_TRACE = b'{"ue_id":null,"complete":false,"count":0,"events":[]}'


class DU:
    def __init__(self):
        self.cells = {}
        self._snap = {"cells": [], "ts": 0}
        self._snap_json = b'{"cells": []}'
        # Single-UE call-flow trace for the dashboard ladder. Mutated only in the
        # event loop (serve_f1); the HTTP thread reads the pre-serialized bytes.
        self.trace_ue = None
        self.trace_events = []
        self.trace_complete = False
        self._trace_json = _EMPTY_TRACE
        self._trace_reset = False

    def _cell(self, cell_id):
        if cell_id not in self.cells:
            self.cells[cell_id] = Cell(cell_id, TOTAL_PRBS)
            log(f"cell {cell_id} brought up with {TOTAL_PRBS} PRBs")
        return self.cells[cell_id]

    def _release_ue(self, ue_id, cell_id):
        """Drop a UE session on one cell and reclaim its PRBs (no-op if absent)."""
        cell = self.cells.get(cell_id)
        if not cell:
            return
        sess = cell.sessions.pop(ue_id, None)
        if sess:
            cell.used_prbs -= sess["prbs"]
            cell.released_total += 1

    def _release_ue_other_cells(self, ue_id, keep_cell_id):
        """A UE may only be admitted on one cell; clear stale sessions elsewhere (handover)."""
        for cid in list(self.cells):
            if cid != keep_cell_id:
                self._release_ue(ue_id, cid)

    # ---- procedure handlers (synchronous == atomic, never await) --------
    #
    # Each handler reads the simulation-functional fields (ue id, serving cell, rf,
    # demand) from the message's `_twin` sidecar — the realistic LTE envelope/decoded
    # body carries the protocol shape, the sidecar carries what real traces lack — and
    # returns a realistic downlink built via the signalling catalog.

    def _reply(self, logical, ue_id, cell_id, step=None, **fields):
        return CATALOG.build(logical, ue_id=ue_id, cell=cell_id, step=step, **fields)

    def handle_setup(self, msg, step=None):
        """Admission control: triggered by the UE's capability info, the network would
        issue an Initial Context Setup Request (the E-RAB/PRB grant) — or reject."""
        tw = twin(msg)
        ue_id = tw["ue_id"]
        cell = self._cell(tw["cell"])
        # Make-before-break HO: target setup arrives before source release — avoid
        # double-counting the same ue_id on two cells.
        self._release_ue_other_cells(ue_id, cell.cell_id)
        self._release_ue(ue_id, cell.cell_id)  # re-attach on same cell
        sinr = tw["rf"]["sinr_dl_db"]
        demand = tw["demand_mbps"]
        required, per_prb, se = rf.prbs_for_traffic(demand, sinr, SCS_KHZ, TRAFFIC_PROFILE)
        if required is None:
            cell.rejected_total += 1
            return self._reply(proc.RRC_CONNECTION_REJECT, ue_id, cell.cell_id, step, cause="no-coverage")
        if required > cell.free_prbs:
            cell.rejected_total += 1
            return self._reply(proc.RRC_CONNECTION_REJECT, ue_id, cell.cell_id, step,
                               cause="insufficient-prb", free_prbs=cell.free_prbs, required_prbs=required)
        cell.used_prbs += required
        cell.admitted_total += 1
        cell.sessions[ue_id] = {
            "ue_id": ue_id, "cell_id": cell.cell_id, "prbs": required,
            "sinr_dl_db": round(sinr, 1), "se": round(se, 2), "per_prb_mbps": per_prb,
            "demand_mbps": demand, "traffic_profile": TRAFFIC_PROFILE, "updated": time.time(),
        }
        return self._reply(proc.S1_INITIAL_CONTEXT_SETUP_REQUEST, ue_id, cell.cell_id, step,
                           allocated_prbs=required, mcs=rf.mcs_from_se(se))

    def handle_measurement(self, msg, step=None):
        tw = twin(msg)
        ue_id = tw["ue_id"]
        cell = self._cell(tw["cell"])
        sess = cell.sessions.get(ue_id)
        if sess is None:
            return self._reply(proc.RRC_CONNECTION_REJECT, ue_id, cell.cell_id, step, cause="unknown-ue")
        sinr = tw["rf"]["sinr_dl_db"]
        profile = sess.get("traffic_profile", TRAFFIC_PROFILE)
        required, per_prb, se = rf.prbs_for_traffic(sess["demand_mbps"], sinr, SCS_KHZ, profile)
        if required is None:
            cell.used_prbs -= sess["prbs"]
            cell.released_total += 1
            del cell.sessions[ue_id]
            return self._reply(proc.RRC_CONNECTION_REJECT, ue_id, cell.cell_id, step, cause="lost-coverage")
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
        return self._reply(proc.RRC_CONNECTION_RECONFIGURATION, ue_id, cell.cell_id, step,
                           allocated_prbs=new_prbs, mcs=rf.mcs_from_se(se), congested=congested)

    def handle_data(self, msg, step=None):
        tw = twin(msg)
        ue_id = tw["ue_id"]
        cell = self._cell(tw["cell"])
        sess = cell.sessions.get(ue_id)
        if sess is None:
            return self._reply(proc.RRC_CONNECTION_REJECT, ue_id, cell.cell_id, step, cause="unknown-ue")
        return self._reply(proc.UE_DATA_ACK, ue_id, cell.cell_id, step,
                           achievable_mbps=round(sess["prbs"] * sess["per_prb_mbps"], 2),
                           demand_mbps=sess["demand_mbps"])

    def handle_release(self, msg, step=None, reason="release"):
        tw = twin(msg)
        ue_id = tw["ue_id"]
        cell = self._cell(tw["cell"])
        self._release_ue(ue_id, cell.cell_id)
        return self._reply(proc.S1_UE_CONTEXT_RELEASE_COMMAND, ue_id, cell.cell_id, step, cause=reason)

    def handle_passthrough(self, msg, step):
        """Intermediate flow steps (rrc setup, security, capability enquiry, reconfig
        complete, release complete): advance the call flow with the mapped downlink,
        no capacity change."""
        tw = twin(msg)
        return self._reply(step.downlink, tw.get("ue_id", "?"), tw.get("cell", "?"), step.name)

    def dispatch(self, msg):
        step = CATALOG.classify(msg)
        if step is None:
            tw = twin(msg)
            return self._reply(proc.RRC_CONNECTION_REJECT, tw.get("ue_id", "?"), tw.get("cell", "?"),
                               cause=f"bad-message:{msg.get('message_name') or msg.get('type')}")
        if step.action == proc.ACT_ADMIT:
            return self.handle_setup(msg, step.name)
        if step.action == proc.ACT_RECONFIG:
            return self.handle_measurement(msg, step.name)
        if step.action == proc.ACT_RELEASE:
            return self.handle_release(msg, step.name)
        if step.name == proc.STEP_DATA:
            return self.handle_data(msg, step.name)
        return self.handle_passthrough(msg, step)

    # ---- single-UE call-flow trace (for the dashboard ladder) ------------

    def request_trace_reset(self):
        """Ask the event loop to drop the current trace and lock onto the next UE."""
        self._trace_reset = True

    def _trace_record(self, m, direction):
        """Capture the realistic messages for ONE UE so the dashboard can draw a live
        sequence ladder. Locks onto a UE at its RRC connection request (clean start),
        follows it to release, then freezes until reset. A rejected attach releases the
        lock so the next UE is tried instead of freezing on a stub."""
        if self._trace_reset:
            self.trace_ue = None
            self.trace_events = []
            self.trace_complete = False
            self._trace_reset = False
            self._trace_json = _EMPTY_TRACE
        if self.trace_complete:
            return
        ue = (m.get("_twin") or {}).get("ue_id")
        if not ue:
            return
        step = CATALOG.classify(m)
        if self.trace_ue is None:
            if direction == "up" and step is not None and step.name == proc.STEP_RRC_SETUP:
                self.trace_ue = ue
                self.trace_events = []
            else:
                return
        if ue != self.trace_ue or len(self.trace_events) >= TRACE_MAX:
            return
        is_reject = m.get("message_name") == CATALOG.real_name(proc.RRC_CONNECTION_REJECT)
        self.trace_events.append({
            "t": time.time(),
            "dir": direction,
            "name": m.get("message_name"),
            "interface": (m.get("interface") or "").upper(),
            "step": step.name if step else None,
            "action": step.action if step else "none",
            "reject": is_reject,
            "message": m,
        })
        if direction == "down" and is_reject:
            # attach refused — abandon this UE and trace the next attacher
            self.trace_ue = None
            self.trace_events = []
            self._trace_json = _EMPTY_TRACE
            return
        if direction == "down" and step is not None and step.name == proc.STEP_RELEASE_COMPLETE:
            self.trace_complete = True
        self._trace_json = json.dumps({
            "ue_id": self.trace_ue,
            "complete": self.trace_complete,
            "count": len(self.trace_events),
            "events": self.trace_events,
            "ts": time.time(),
        }).encode()

    # ---- F1 connection (one per RU, multiplexed over all its UEs) --------

    async def serve_f1(self, reader, writer):
        peer = writer.get_extra_info("peername")
        conn_cells = set()
        log(f"F1 link up from RU {peer}")
        try:
            while True:
                msg = await P.async_recv_msg(reader)
                serving = (msg.get("_twin") or {}).get("cell")
                if serving:
                    conn_cells.add(serving)
                self._trace_record(msg, "up")
                reply = self.dispatch(msg)          # atomic: no await inside
                reply["txn"] = msg.get("txn")
                self._trace_record(reply, "down")
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
                    self._release_ue(uid, cid)
            if freed:
                log(f"F1 link {peer} down: reclaimed {freed} PRBs")
            writer.close()

    # ---- observability --------------------------------------------------

    def build_snapshot(self):
        cells = []
        unique_ids = set()
        for c in self.cells.values():
            unique_ids.update(c.sessions)
            cells.append({
                "cell_id": c.cell_id, "total_prbs": c.total_prbs,
                "used_prbs": c.used_prbs, "free_prbs": c.free_prbs,
                "utilization": round(c.used_prbs / c.total_prbs, 3) if c.total_prbs else 0,
                "connected_ues": len(c.sessions),
                "admitted_total": c.admitted_total,
                "rejected_total": c.rejected_total,
                "released_total": c.released_total,
            })
        return {
            "cells": cells,
            "unique_connected_ues": len(unique_ids),
            "ts": time.time(),
        }

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
            elif self.path.startswith("/trace/reset"):
                du.request_trace_reset()
                body = b'{"ok":true}'
                ctype = "application/json"
            elif self.path.startswith("/trace"):
                body = du._trace_json
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
