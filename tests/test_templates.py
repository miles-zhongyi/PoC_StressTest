"""Template engine + catalog round-trip tests (no sockets)."""
import json

import pytest

from common.signaling import get_catalog
from common.signaling import procedures as proc
from common.signaling.templates import abstract_record, fill, tok

ROOT_DATA = "data/lte_templates.json"


def _has_unfilled_tokens(obj) -> bool:
    return "<<" in json.dumps(obj)


def test_fill_replaces_tokens_and_keeps_structure():
    template = {"a": tok("m_tmsi"), "b": {"c": [tok("cell_id"), "literal"]}, "hex": "deadbeef"}
    out = fill(template, {"m_tmsi": 42, "cell_id": 7})
    assert out == {"a": 42, "b": {"c": [7, "literal"]}, "hex": "deadbeef"}
    # original template is untouched (deep copy)
    assert template["a"] == tok("m_tmsi")


def test_abstract_record_tokenizes_envelope_keeps_decoded():
    rec = {"message_name": "X", "m_tmsi": 123, "cell_id": 71, "timestamp": "t",
           "decoded": {"message": ["choice", {"hex": "abc"}]}}
    t = abstract_record(rec)
    assert t["m_tmsi"] == tok("m_tmsi")
    assert t["cell_id"] == tok("cell_id")
    assert t["message_name"] == "X"                 # not abstracted
    assert t["decoded"] == rec["decoded"]           # body kept verbatim


@pytest.mark.parametrize("templates_path", [None, ROOT_DATA])
def test_build_classify_roundtrip_all_steps(templates_path):
    """Every uplink step builds a fully-filled realistic message that the catalog
    classifies back to the same step — with built-in defaults and with real templates."""
    cat = get_catalog("lte", templates_path=templates_path)
    for step in proc.ALL_STEPS:
        msg = cat.build(step.uplink, ue_id="ue-test-00001", cell="cell-1",
                        txn=3, step=step.name, position={"x": 1, "y": 2},
                        tx_power_dbm=23.0, demand_mbps=0.03)
        assert not _has_unfilled_tokens(msg), f"unfilled token in {step.uplink}: {msg}"
        assert msg["message_name"] == cat.real_name(step.uplink)
        assert "decoded" in msg
        got = cat.classify(msg)
        assert got is not None and got.name == step.name


def test_downlink_messages_build_clean():
    cat = get_catalog("lte")
    for logical in (proc.RRC_CONNECTION_SETUP, proc.S1_INITIAL_CONTEXT_SETUP_REQUEST,
                    proc.RRC_CONNECTION_RECONFIGURATION, proc.RRC_CONNECTION_REJECT,
                    proc.S1_UE_CONTEXT_RELEASE_COMMAND, proc.RRC_CONNECTION_RELEASE):
        msg = cat.build(logical, ue_id="ue-x", cell="cell-2", allocated_prbs=2, mcs=20)
        assert not _has_unfilled_tokens(msg)
        assert msg["message_name"] == cat.real_name(logical)


def test_real_message_names_are_lte():
    """Sanity: the wire really uses real RRC/S1AP names, not the old stand-ins."""
    cat = get_catalog("lte")
    assert cat.real_name(proc.RRC_CONNECTION_SETUP_COMPLETE) == "RRC_RRC_CONNECTION_SETUP_COMPLETE"
    assert cat.real_name(proc.S1_INITIAL_CONTEXT_SETUP_REQUEST) == "S1_INITIAL_CONTEXT_SETUP_REQUEST"
    assert cat.real_name(proc.S1_UE_CONTEXT_RELEASE_REQUEST) == "S1_UE_CONTEXT_RELEASE_REQUEST"


def test_unknown_tech_raises():
    with pytest.raises(ValueError):
        get_catalog("zigbee")


def test_nr_not_implemented():
    with pytest.raises(NotImplementedError):
        get_catalog("nr")


def test_stable_ids_are_deterministic():
    cat = get_catalog("lte")
    a = cat.build(proc.RRC_CONNECTION_SETUP_COMPLETE, ue_id="ue-7", cell="cell-1")
    b = cat.build(proc.RRC_CONNECTION_SETUP_COMPLETE, ue_id="ue-7", cell="cell-1")
    assert a["m_tmsi"] == b["m_tmsi"]               # same ue -> same m-TMSI across builds
