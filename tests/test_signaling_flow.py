"""DU call-flow tests: drive DU.dispatch() with catalog-built messages (no sockets)."""
import pytest

from common.signaling import get_catalog, twin
from common.signaling import procedures as proc
from du.du_server import DU

CAT = get_catalog("lte")
CELL = "cell-1"


def ue_uplink(step, ue_id, sinr_db, **fields):
    """Build a UE uplink for `step` and apply what the RU would stamp (serving cell
    + computed RF) so the DU can process it."""
    msg = CAT.build(step.uplink, ue_id=ue_id, cell=CELL, step=step.name,
                    position={"x": 10, "y": 0}, tx_power_dbm=23.0, **fields)
    tw = msg["_twin"]
    tw["cell"] = CELL
    tw["rf"] = {"sinr_dl_db": sinr_db, "sinr_ul_db": sinr_db, "rsrp_dl_dbm": -90.0}
    return msg


def run_attach(du, ue_id, sinr_db, demand=0.03):
    """Walk the full attach flow through dispatch; return the admission reply."""
    admission = None
    for step in CAT.attach_flow():
        d = {"demand_mbps": demand} if step.action == proc.ACT_ADMIT else {}
        reply = du.dispatch(ue_uplink(step, ue_id, sinr_db, **d))
        if step.action == proc.ACT_ADMIT:
            admission = reply
    return admission


def test_full_attach_allocates_prbs_with_real_names():
    du = DU()
    seen_names = []
    for step in CAT.attach_flow():
        d = {"demand_mbps": 0.03} if step.action == proc.ACT_ADMIT else {}
        reply = du.dispatch(ue_uplink(step, "ue-a", 20.0, **d))
        seen_names.append(reply["message_name"])

    cell = du.cells[CELL]
    assert cell.admitted_total == 1
    assert cell.used_prbs >= 1                       # VoIP reserves 1-2 PRBs
    assert cell.sessions["ue-a"]["prbs"] == cell.used_prbs
    # the admission reply is a real S1AP Initial Context Setup Request carrying the grant
    assert "S1_INITIAL_CONTEXT_SETUP_REQUEST" in seen_names
    # intermediate replies use real RRC names, not the old stand-ins
    assert "RRC_SECURITY_MODE_COMMAND" in seen_names
    assert "RRC_RRC_CONNECTION_SETUP" in seen_names


def test_no_coverage_is_rejected():
    du = DU()
    admission = run_attach(du, "ue-edge", sinr_db=-20.0)   # below MIN_SINR -> no coverage
    assert twin(admission)["logical"] == proc.RRC_CONNECTION_REJECT
    assert twin(admission)["cause"] == "no-coverage"
    assert du.cells[CELL].used_prbs == 0


def test_measurement_reconfig_then_release_reclaims_prbs():
    du = DU()
    run_attach(du, "ue-m", sinr_db=20.0)
    used_after_attach = du.cells[CELL].used_prbs
    assert used_after_attach >= 1

    # measurement at marginal RF -> reconfiguration (may bump VoIP grant to 2 PRBs)
    reply = du.dispatch(ue_uplink(proc.MEASUREMENT_STEP, "ue-m", sinr_db=2.0))
    assert reply["message_name"] == "RRC_RRC_CONNECTION_RECONFIGURATION"
    assert twin(reply)["allocated_prbs"] >= 1

    # release request reclaims PRBs
    rel = du.dispatch(ue_uplink(proc.RELEASE_FLOW[0], "ue-m", sinr_db=20.0))
    assert rel["message_name"] == "S1_UE_CONTEXT_RELEASE_COMMAND"
    assert du.cells[CELL].used_prbs == 0
    assert "ue-m" not in du.cells[CELL].sessions


def test_measurement_for_unknown_ue_rejected():
    du = DU()
    reply = du.dispatch(ue_uplink(proc.MEASUREMENT_STEP, "ghost", sinr_db=20.0))
    assert twin(reply)["logical"] == proc.RRC_CONNECTION_REJECT
    assert twin(reply)["cause"] == "unknown-ue"


def test_capacity_exhaustion_rejects(monkeypatch):
    """Fill the pool, then a further UE is rejected for insufficient PRBs."""
    du = DU()
    cell = du._cell(CELL)
    cell.total_prbs = 2                              # shrink so one VoIP UE fills it
    run_attach(du, "ue-1", sinr_db=2.0)              # marginal RF -> 2 PRBs
    assert cell.free_prbs == 0
    admission = run_attach(du, "ue-2", sinr_db=2.0)
    assert twin(admission)["logical"] == proc.RRC_CONNECTION_REJECT
    assert twin(admission)["cause"] == "insufficient-prb"


def test_handover_make_before_break_no_double_count():
    """Admitting the same UE on cell-2 while still on cell-1 must not double-count."""
    du = DU()
    run_attach(du, "ue-ho", sinr_db=20.0)
    assert du.cells[CELL].used_prbs >= 1

    # build an attach on cell-2 for the same UE
    other = "cell-2"
    for step in CAT.attach_flow():
        d = {"demand_mbps": 0.03} if step.action == proc.ACT_ADMIT else {}
        msg = CAT.build(step.uplink, ue_id="ue-ho", cell=other, step=step.name,
                        position={"x": 10, "y": 0}, tx_power_dbm=23.0, **d)
        msg["_twin"]["cell"] = other
        msg["_twin"]["rf"] = {"sinr_dl_db": 20.0}
        du.dispatch(msg)

    # source cell session cleared, only the target holds the UE
    assert "ue-ho" not in du.cells[CELL].sessions
    assert "ue-ho" in du.cells[other].sessions
    assert du.cells[CELL].used_prbs == 0
