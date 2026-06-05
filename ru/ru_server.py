"""
RU (Radio Unit) — asyncio, built to scale.
==========================================

The RU is the radio: it turns UE geometry/power into RSRP & SINR and forwards
that to the DU. What changed for scale:

  * It is now an asyncio server, so it can hold thousands of UE links (one socket
    per UE — a UE's socket *is* its radio link, which is the right abstraction for
    the upcoming handover step).
  * Toward the DU it keeps ONE multiplexed F1 connection. Each UE request is
    tagged with a transaction id; a single reader task matches DU replies back to
    the waiting request via a future. This is the standard async RPC-over-one-
    socket pattern and keeps DU connection count at 1 per RU.
  * If a UE drops its link without releasing, the RU sends a synthetic
    RRC_RELEASE to the DU so PRBs are never leaked under churn.
"""
import asyncio
import itertools
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
from common.ru_dictionary import RuDictionary, parse_sectors_env
from common.signaling import get_catalog, twin
from common.signaling import procedures as proc

CATALOG = get_catalog()

TCP_PORT = int(os.environ.get("RU_TCP_PORT", "38470"))
DU_HOST = os.environ.get("DU_HOST", "127.0.0.1")
DU_PORT = int(os.environ.get("DU_PORT", "38472"))
CELL_ID = os.environ.get("CELL_ID", "cell-1")
SITE_ID = os.environ.get("SITE_ID", CELL_ID)
TX_POWER_DBM = float(os.environ.get("TX_POWER_DBM", "49"))
TX_GAIN_DB = float(os.environ.get("TX_GAIN_DB", "15"))
FREQ_GHZ = float(os.environ.get("FREQ_GHZ", "3.5"))
BANDWIDTH_MHZ = float(os.environ.get("BANDWIDTH_MHZ", "100"))
CELL_TOTAL_PRBS = int(os.environ.get("CELL_TOTAL_PRBS", os.environ.get("TOTAL_PRBS", "250")))
RU_X = float(os.environ.get("RU_X", "0"))
RU_Y = float(os.environ.get("RU_Y", "0"))
SECTOR_WIDTH_DEG = float(os.environ.get("SECTOR_WIDTH_DEG", "120"))
RU_STATE_PATH = os.environ.get(
    "RU_STATE_PATH",
    f"/trace/data/ru_state/{SITE_ID}.json",
)
RU_STATE_FLUSH_SEC = float(os.environ.get("RU_STATE_FLUSH_SEC", "1"))
RU_HTTP_PORT = int(os.environ.get("RU_HTTP_PORT", "8082"))
_BW_HZ = BANDWIDTH_MHZ * 1e6

SECTOR_CFG = parse_sectors_env(
    bandwidth_mhz=BANDWIDTH_MHZ,
    default_freq_ghz=FREQ_GHZ,
    total_prbs=CELL_TOTAL_PRBS,
)
SECTORS = {c: cfg["azimuth_deg"] for c, cfg in SECTOR_CFG.items()}
_CELL_NUM = {c: CATALOG.cell_num(c) for c in SECTOR_CFG}

RU_DICT = RuDictionary.from_env()
_dict_json = b"{}"


def log(msg):
    print(f"[RU {SITE_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_rf(ue_pos, cell_id, ue_tx_power_dbm):
    """RF snapshot for the sector identified by `cell_id` on this site."""
    cfg = SECTOR_CFG.get(cell_id, {})
    freq = cfg.get("freq_ghz", FREQ_GHZ)
    bw_hz = float(cfg.get("bandwidth_mhz", BANDWIDTH_MHZ)) * 1e6
    return rf.link_rf(
        ue_pos, RU_X, RU_Y, TX_POWER_DBM, freq, bw_hz,
        tx_gain_db=TX_GAIN_DB, ue_tx_power_dbm=ue_tx_power_dbm,
        azimuth_deg=SECTORS.get(cell_id), sector_width_deg=SECTOR_WIDTH_DEG,
    )


def best_sector(ue_pos):
    """Strongest of this site's sectors for a position (fallback when the UE's chosen
    cell isn't one of ours)."""
    return max(SECTORS, key=lambda c: compute_rf(ue_pos, c, 23.0)["rsrp_dl_dbm"])


class F1Link:
    """Single multiplexed F1 connection to the DU with txn-correlated replies."""

    def __init__(self):
        self.reader = None
        self.writer = None
        self.pending = {}
        self.txns = itertools.count(1)
        self.wlock = asyncio.Lock()
        self._reader_task = None
        self._reconnect_task = None

    def _fail_pending(self, exc):
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()

    async def connect(self, retries=60, delay=1.0):
        for attempt in range(retries):
            try:
                self.reader, self.writer = await asyncio.open_connection(DU_HOST, DU_PORT)
                if self._reader_task is None or self._reader_task.done():
                    self._reader_task = asyncio.create_task(self._reader_loop())
                log(f"F1 link established to DU {DU_HOST}:{DU_PORT}")
                return
            except OSError:
                if attempt == 0:
                    log(f"waiting for DU at {DU_HOST}:{DU_PORT} ...")
                await asyncio.sleep(delay)
        raise ConnectionError("DU unreachable")

    async def _reader_loop(self):
        try:
            while True:
                reply = await P.async_recv_msg(self.reader)
                fut = self.pending.pop(reply.get("txn"), None)
                if fut and not fut.done():
                    fut.set_result(reply)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self._fail_pending(ConnectionError("F1 link down"))
            self.reader = None
            self.writer = None
            log("F1 link to DU lost — reconnecting (Uu stays up)")
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        delay = 0.5
        while self.writer is None:
            try:
                await self.connect(retries=1, delay=0.2)
                return
            except ConnectionError:
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 5.0)

    async def _ensure_connected(self):
        if self.writer is not None:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            await self._reconnect_task
        elif self.writer is None:
            await self._reconnect_loop()

    async def request(self, msg):
        await self._ensure_connected()
        txn = next(self.txns)
        fut = asyncio.get_event_loop().create_future()
        self.pending[txn] = fut
        msg["txn"] = txn
        try:
            async with self.wlock:
                await P.async_send_msg(self.writer, msg)
            return await fut
        except (ConnectionError, OSError, asyncio.IncompleteReadError) as exc:
            self._fail_pending(exc)
            self.reader = None
            self.writer = None
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())
            raise


F1 = F1Link()


def _refresh_dict_snapshot():
    global _dict_json
    _dict_json = json.dumps(RU_DICT.to_dict(), indent=2).encode()


def _flush_dict_file():
    try:
        RU_DICT.save(RU_STATE_PATH)
    except OSError as exc:
        log(f"ru_state write failed: {exc}")


async def dict_publisher():
    """Periodically mirror the in-memory dictionary to disk."""
    while True:
        await asyncio.sleep(RU_STATE_FLUSH_SEC)
        _refresh_dict_snapshot()
        _flush_dict_file()


def start_http():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path.startswith("/dictionary"):
                body = _dict_json
                ctype = "application/json"
            else:
                body = (
                    f"RU site {SITE_ID}\n"
                    f"dictionary GET /dictionary\n"
                    f"state file {RU_STATE_PATH}\n"
                ).encode()
                ctype = "text/plain"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("0.0.0.0", RU_HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def serve_ue(reader, writer):
    """One Uu socket per UE. The RU is the radio: it stamps the serving cell and the
    RF it computes from the UE's geometry into the message's `_twin` sidecar, then
    transparently proxies the realistic signalling to the DU over the multiplexed F1
    link and relays the reply back."""
    ue_id = None
    cell = next(iter(SECTORS))                           # default until the UE tells us
    released = False
    try:
        while True:
            msg = await P.async_recv_msg(reader)        # uplink from UE
            tw = msg.get("_twin") or {}
            ue_id = tw.get("ue_id", ue_id)
            pos = tw.get("position")
            # The UE picks which of this site's sectors it is on; trust it, but fall
            # back to the strongest sector for the UE's position if it's not ours.
            chosen = tw.get("cell")
            if chosen in SECTORS:
                cell = chosen
            elif pos is not None:
                cell = best_sector(pos)
            tw["cell"] = cell                            # functional: the serving cell
            msg["cell_id"] = _CELL_NUM.get(cell, 0)      # cosmetic (realistic envelope)
            if pos is not None:
                tw["rf"] = compute_rf(pos, cell, tw.get("tx_power_dbm", 23.0))
            msg["_twin"] = tw
            RU_DICT.note_uplink(ue_id, cell, tw)
            reply = await F1.request(msg)               # multiplexed to DU
            RU_DICT.note_downlink(ue_id, cell, twin(reply))
            await P.async_send_msg(writer, reply)       # downlink to UE
            if CATALOG.is_final_uplink(msg):            # UE completed its release
                RU_DICT.remove_ue(ue_id)
                released = True
                break
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        if ue_id and not released:                      # UE vanished -> free PRBs
            RU_DICT.remove_ue(ue_id)
            try:
                await F1.request(CATALOG.build(proc.S1_UE_CONTEXT_RELEASE_REQUEST,
                                               ue_id=ue_id, cell=cell,
                                               step=proc.STEP_RELEASE_REQUEST))
            except (ConnectionError, OSError):
                pass
        try:
            writer.close()
        except OSError:
            pass


async def main():
    await F1.connect()
    _refresh_dict_snapshot()
    _flush_dict_file()
    start_http()
    asyncio.create_task(dict_publisher())
    server = await asyncio.start_server(serve_ue, "0.0.0.0", TCP_PORT, backlog=1024)
    sectors = ", ".join(
        f"{c}@{('omni' if SECTORS[c] is None else f'{SECTORS[c]:.0f}°')}" for c in SECTORS
    )
    log(f"site {SITE_ID} ({RU_DICT.ru_type}) at ({RU_X},{RU_Y}): "
        f"{FREQ_GHZ} GHz / {BANDWIDTH_MHZ} MHz, {RU_DICT.num_cells} cells @ "
        f"{CELL_TOTAL_PRBS} PRB/cell | sectors: {sectors}")
    log(f"ru_state -> {RU_STATE_PATH}  dictionary http://0.0.0.0:{RU_HTTP_PORT}/dictionary")
    log(f"listening on :{TCP_PORT} (Uu), backhaul F1 -> {DU_HOST}:{DU_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down")
