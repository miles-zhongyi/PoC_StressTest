"""
RU cluster topology: each RU site flattens into 3 sector cells, and the UE selects an
in-sector cell on the nearest site. Tests the pure parsing/selection helpers (no module
reload, so no cross-test contamination of the shared ue/ru globals).
"""
import json
import os

from ue import ue_sim as u

RU_LIST = [
    {"name": "RU1", "host": "ru", "port": 38470, "x": 0, "y": 600,
     "sectors": [{"cell": "RU1-A", "azimuth": 60}, {"cell": "RU1-B", "azimuth": 180}, {"cell": "RU1-C", "azimuth": 300}]},
    {"name": "RU2", "host": "ru2", "port": 38470, "x": -520, "y": -300,
     "sectors": [{"cell": "RU2-A", "azimuth": 60}, {"cell": "RU2-B", "azimuth": 180}, {"cell": "RU2-C", "azimuth": 300}]},
]


def _parse(monkeypatch):
    monkeypatch.setenv("RU_LIST", json.dumps(RU_LIST))
    return u._parse_rus()


def test_sites_flatten_into_sector_links(monkeypatch):
    links = _parse(monkeypatch)
    assert len(links) == 6                        # 2 sites x 3 sectors
    assert {r["name"] for r in links} == {"RU1-A", "RU1-B", "RU1-C", "RU2-A", "RU2-B", "RU2-C"}
    a = next(r for r in links if r["name"] == "RU1-A")
    assert (a["host"], a["port"], a["x"], a["y"], a["site"], a["azimuth_deg"]) == ("ru", 38470, 0.0, 600.0, "RU1", 60)
    # all three RU1 sectors share the site coords/host
    ru1 = [r for r in links if r["site"] == "RU1"]
    assert {(r["host"], r["x"], r["y"]) for r in ru1} == {("ru", 0.0, 600.0)}


def test_best_cell_is_in_sector_on_nearest_site(monkeypatch):
    links = _parse(monkeypatch)
    best_at = lambda pos: max(links, key=lambda r: u.rsrp_from(r, pos))
    assert best_at({"x": 0, "y": 600})["site"] == "RU1"        # at RU1
    assert best_at({"x": -520, "y": -250})["site"] == "RU2"    # near RU2
    # the chosen cell actually covers the UE (in its 120° sector)
    import common.rf_model as rf
    b = best_at({"x": 0, "y": 550})
    snap = rf.link_rf({"x": 0, "y": 550}, b["x"], b["y"], b["tx_power_dbm"], b["freq_ghz"],
                      100e6, azimuth_deg=b["azimuth_deg"])
    assert snap["in_sector"] is True


def test_legacy_single_sector_entry_still_parses(monkeypatch):
    monkeypatch.setenv("RU_LIST", json.dumps([{"name": "cell-1", "host": "ru", "port": 38470, "x": 0, "y": 0}]))
    links = u._parse_rus()
    assert len(links) == 1 and links[0]["name"] == "cell-1" and links[0]["azimuth_deg"] is None
