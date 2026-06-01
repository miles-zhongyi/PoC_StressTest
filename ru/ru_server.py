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

    async def connect(self, retries=60, delay=1.0):
        for attempt in range(retries):
            try:
                self.reader, self.writer = await asyncio.open_connection(DU_HOST, DU_PORT)
                asyncio.create_task(self._reader_loop())
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
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("F1 link down"))
            self.pending.clear()
            log("F1 link to DU lost — exiting for restart")
            os._exit(1)   # let the container manager restart us cleanly

    async def request(self, msg):
        txn = next(self.txns)
        fut = asyncio.get_event_loop().create_future()
        self.pending[txn] = fut
        msg["txn"] = txn
        async with self.wlock:
            await P.async_send_msg(self.writer, msg)
        return await fut


F1 = F1Link()


async def serve_ue(reader, writer):
    ue_id = None
    released = False
    try:
        while True:
            msg = await P.async_recv_msg(reader)        # uplink from UE
            ue_id = msg.get("ue_id", ue_id)
            msg["cell_id"] = CELL_ID
            if "position" in msg:
                msg["rf"] = compute_rf(msg["position"], msg.get("tx_power_dbm", 23.0))
            reply = await F1.request(msg)               # multiplexed to DU
            await P.async_send_msg(writer, reply)       # downlink to UE
            if msg.get("type") == P.RRC_RELEASE:
                released = True
                break
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass
    finally:
        if ue_id and not released:                      # UE vanished -> free PRBs
            try:
                await F1.request({"type": P.RRC_RELEASE, "ue_id": ue_id, "cell_id": CELL_ID})
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
