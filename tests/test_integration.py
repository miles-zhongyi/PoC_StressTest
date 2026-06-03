"""
End-to-end in-process smoke test: real UE -> RU -> DU over localhost sockets.

Env must be set before importing the server modules (they read ports/geometry at
import), so this runs at module load. No pytest-asyncio needed — each test drives
its own event loop via asyncio.run().
"""
import asyncio
import os
import socket


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


DU_PORT = _free_port()
RU_PORT = _free_port()
os.environ.update(
    DU_HOST="127.0.0.1", DU_PORT=str(DU_PORT), DU_TCP_PORT=str(DU_PORT),
    RU_TCP_PORT=str(RU_PORT), RU_HOST="127.0.0.1", RU_PORT=str(RU_PORT),
    CELL_ID="cell-1", RU_X="0", RU_Y="0",
    RADIO_TECH="lte",
    SESSION_DURATION="1", REPORT_INTERVAL="0.3", DATA_INTERVAL="0.3",
    RAMP_SECONDS="0", IDLE_BETWEEN="0", START_RADIUS_M="150", MAX_RADIUS_M="250",
)
os.environ.pop("RU_LIST", None)  # single RU at origin

import du.du_server as du_mod      # noqa: E402
import ru.ru_server as ru_mod      # noqa: E402
import ue.ue_sim as ue_mod         # noqa: E402


async def _run_stack(num_ues=5):
    du = du_mod.DU()
    received = []
    inner = du.dispatch

    def recording_dispatch(msg):
        received.append(msg.get("message_name"))
        return inner(msg)

    du.dispatch = recording_dispatch

    # Fresh F1 link each run (futures must belong to this event loop).
    ru_mod.F1 = ru_mod.F1Link()
    du_srv = await asyncio.start_server(du.serve_f1, "127.0.0.1", DU_PORT)
    await ru_mod.F1.connect()
    ru_srv = await asyncio.start_server(ru_mod.serve_ue, "127.0.0.1", RU_PORT)
    try:
        await asyncio.wait_for(
            asyncio.gather(*(ue_mod.one_session(f"ue-{i:03d}", 0.03, 23.0, ue_mod.Walk(1.0))
                             for i in range(num_ues))),
            timeout=25,
        )
    finally:
        # Tear down the RU's persistent F1 link first, otherwise the DU server's
        # wait_closed() would block on that still-open connection (Python 3.12).
        if ru_mod.F1.writer is not None:
            ru_mod.F1.writer.close()
        if ru_mod.F1._reader_task is not None:
            ru_mod.F1._reader_task.cancel()
        du_srv.close()
        ru_srv.close()
    return du, received


def test_stack_admits_uses_and_releases_prbs():
    du, received = asyncio.run(_run_stack(num_ues=5))
    cell = du.cells.get("cell-1")
    assert cell is not None
    assert cell.admitted_total >= 1                  # at least some UEs attached
    # graceful releases at end of each 1s session -> PRBs reclaimed
    assert cell.used_prbs == 0
    assert cell.sessions == {}


def test_stack_exchanges_real_lte_message_names():
    _, received = asyncio.run(_run_stack(num_ues=3))
    # the wire carried real RRC/S1AP names through the full attach + release flow
    assert "RRC_RRC_CONNECTION_REQUEST" in received
    assert "RRC_RRC_CONNECTION_SETUP_COMPLETE" in received
    assert "RRC_SECURITY_MODE_COMPLETE" in received
    assert "RRC_UE_CAPABILITY_INFORMATION" in received
    assert "S1_UE_CONTEXT_RELEASE_REQUEST" in received
    # none of the old stand-in type names should appear
    assert "RRC_SETUP_REQUEST" not in received
