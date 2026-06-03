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
import math
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.isdir(os.path.join(_p, "common")):
        sys.path.insert(0, _p)
        break

from common import protocol as P
from common import rf_model as rf
from common.signaling import get_catalog
from common.signaling import procedures as proc

CATALOG = get_catalog()

TCP_PORT = int(os.environ.get("RU_TCP_PORT", "38470"))
DU_HOST = os.environ.get("DU_HOST", "127.0.0.1")
DU_PORT = int(os.environ.get("DU_PORT", "38472"))
CELL_ID = os.environ.get("CELL_ID", "cell-1")
TX_POWER_DBM = float(os.environ.get("TX_POWER_DBM", "49"))
TX_GAIN_DB = float(os.environ.get("TX_GAIN_DB", "15"))
FREQ_GHZ = float(os.environ.get("FREQ_GHZ", "3.5"))
BANDWIDTH_MHZ = float(os.environ.get("BANDWIDTH_MHZ", "100"))
RU_X = float(os.environ.get("RU_X", "0"))
RU_Y = float(os.environ.get("RU_Y", "0"))
_BW_HZ = BANDWIDTH_MHZ * 1e6
_CELL_NUM = CATALOG.cell_num(CELL_ID)  # cosmetic numeric cell id for the realistic envelope


def log(msg):
    print(f"[RU {CELL_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compute_rf(ue_pos, ue_tx_power_dbm):
    dx = ue_pos["x"] - RU_X
    dy = ue_pos["y"] - RU_Y
    distance = max(1.0, math.hypot(dx, dy))
    return {
        "distance_m": round(distance, 1),
        "rsrp_dl_dbm": round(rf.rsrp_dbm(TX_POWER_DBM, distance, FREQ_GHZ, TX_GAIN_DB), 1),
        "sinr_dl_db": round(rf.sinr_db(TX_POWER_DBM, distance, FREQ_GHZ, _BW_HZ,
                                       tx_gain_db=TX_GAIN_DB), 1),
        "sinr_ul_db": round(rf.sinr_db(ue_tx_power_dbm, distance, FREQ_GHZ, _BW_HZ,
                                       tx_gain_db=0.0, rx_gain_db=TX_GAIN_DB), 1),
    }


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


async def serve_ue(reader, writer):
    """One Uu socket per UE. The RU is the radio: it stamps the serving cell and the
    RF it computes from the UE's geometry into the message's `_twin` sidecar, then
    transparently proxies the realistic signalling to the DU over the multiplexed F1
    link and relays the reply back."""
    ue_id = None
    released = False
    try:
        while True:
            msg = await P.async_recv_msg(reader)        # uplink from UE
            tw = msg.get("_twin") or {}
            ue_id = tw.get("ue_id", ue_id)
            msg["cell_id"] = _CELL_NUM                  # cosmetic (realistic envelope)
            tw["cell"] = CELL_ID                         # functional: the serving cell
            pos = tw.get("position")
            if pos is not None:
                tw["rf"] = compute_rf(pos, tw.get("tx_power_dbm", 23.0))
            msg["_twin"] = tw
            reply = await F1.request(msg)               # multiplexed to DU
            await P.async_send_msg(writer, reply)       # downlink to UE
            if CATALOG.is_final_uplink(msg):            # UE completed its release
                released = True
                break
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        if ue_id and not released:                      # UE vanished -> free PRBs
            try:
                await F1.request(CATALOG.build(proc.S1_UE_CONTEXT_RELEASE_REQUEST,
                                               ue_id=ue_id, cell=CELL_ID,
                                               step=proc.STEP_RELEASE_REQUEST))
            except (ConnectionError, OSError):
                pass
        try:
            writer.close()
        except OSError:
            pass


async def main():
    await F1.connect()
    server = await asyncio.start_server(serve_ue, "0.0.0.0", TCP_PORT, backlog=1024)
    log(f"up: {FREQ_GHZ} GHz, {BANDWIDTH_MHZ} MHz, TX {TX_POWER_DBM} dBm "
        f"(+{TX_GAIN_DB} dBi) at ({RU_X},{RU_Y})")
    log(f"listening on :{TCP_PORT} (Uu), backhaul F1 -> {DU_HOST}:{DU_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutting down")
