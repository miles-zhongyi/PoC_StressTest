"""Tests for per-RU state dictionary."""
import json
import time

from common.ru_dictionary import RuDictionary, nr_band_from_freq_ghz, parse_sectors_env


def test_nr_band_n78():
    assert nr_band_from_freq_ghz(3.5) == "n78"


def test_dictionary_static_fields():
    d = RuDictionary(
        site_id="RU1",
        ru_type="macro-3-sector-O-RU",
        bandwidth_mhz=100,
        default_freq_ghz=3.5,
        position={"x": 0, "y": 600},
        sectors={
            "RU1-A": {"azimuth_deg": 60, "total_prbs": 250, "freq_ghz": 3.5},
            "RU1-B": {"azimuth_deg": 180, "total_prbs": 250, "freq_ghz": 3.5},
            "RU1-C": {"azimuth_deg": 300, "total_prbs": 250, "freq_ghz": 3.5},
        },
        total_prbs_per_cell=250,
    )
    doc = d.to_dict()
    assert doc["site_id"] == "RU1"
    assert doc["ru_type"] == "macro-3-sector-O-RU"
    assert doc["bandwidth_mhz"] == 100
    assert doc["num_cells"] == 3
    assert doc["cells"]["RU1-A"]["total_prbs"] == 250
    assert doc["frequency_band"] == "n78"


def test_ue_frequency_and_prb_update():
    d = RuDictionary(
        site_id="RU2",
        ru_type="macro-3-sector-O-RU",
        bandwidth_mhz=100,
        default_freq_ghz=3.5,
        position={"x": -520, "y": -300},
        sectors={"RU2-A": {"azimuth_deg": 60, "total_prbs": 250, "freq_ghz": 3.48}},
        total_prbs_per_cell=250,
    )
    d.note_uplink("ue-1", "RU2-A", {"rf": {"rsrp_dl_dbm": -85, "sinr_dl_db": 12}})
    d.note_downlink("ue-1", "RU2-A", {"allocated_prbs": 2, "mcs": 18})
    row = d.to_dict()["ues"]["ue-1"]
    assert row["frequency_ghz"] == 3.48
    assert row["frequency_band"] == "n78"
    assert row["allocated_prbs"] == 2
    cells = d.to_dict()["cells"]["RU2-A"]
    assert cells["used_prbs"] == 2
    assert cells["connected_ues"] == 1
    assert cells["free_prbs"] == 248


def test_handover_updates_frequency_band(monkeypatch):
    monkeypatch.setenv(
        "SECTORS",
        json.dumps([
            {"cell": "A", "azimuth": 0, "freq_ghz": 3.5},
            {"cell": "B", "azimuth": 120, "freq_ghz": 2.6},
        ]),
    )
    sectors = parse_sectors_env(bandwidth_mhz=100, default_freq_ghz=3.5, total_prbs=250)
    d = RuDictionary(
        site_id="T",
        ru_type="test",
        bandwidth_mhz=100,
        default_freq_ghz=3.5,
        position={"x": 0, "y": 0},
        sectors=sectors,
        total_prbs_per_cell=250,
    )
    d.note_uplink("ue-x", "A", {})
    d.note_downlink("ue-x", "A", {"allocated_prbs": 1})
    d.note_uplink("ue-x", "B", {})
    d.note_downlink("ue-x", "B", {"allocated_prbs": 1})
    row = d.to_dict()["ues"]["ue-x"]
    assert row["cell"] == "B"
    assert row["frequency_ghz"] == 2.6
    assert row["frequency_band"] == "n41"


def test_save_roundtrip(tmp_path):
    path = tmp_path / "RU1.json"
    d = RuDictionary(
        site_id="RU1",
        ru_type="macro-3-sector-O-RU",
        bandwidth_mhz=100,
        default_freq_ghz=3.5,
        position={"x": 0, "y": 0},
        sectors={"RU1-A": {"azimuth_deg": 60, "total_prbs": 250}},
        total_prbs_per_cell=250,
    )
    d.save(path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["num_cells"] == 1
    assert loaded["ru_type"] == "macro-3-sector-O-RU"
