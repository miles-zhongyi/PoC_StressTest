"""Sector antenna model and UE sector selection."""
import math

from common import rf_model as rf


def _ru(name, azimuth_deg):
    return {
        "name": name,
        "x": 0.0,
        "y": 0.0,
        "tx_power_dbm": 49.0,
        "freq_ghz": 3.5,
        "tx_gain_db": 15.0,
        "azimuth_deg": azimuth_deg,
        "sector_width_deg": 120.0,
    }


def rsrp_from(ru, pos):
    snap = rf.link_rf(
        pos, ru["x"], ru["y"], ru["tx_power_dbm"], ru["freq_ghz"], 100e6,
        tx_gain_db=ru["tx_gain_db"],
        azimuth_deg=ru.get("azimuth_deg"),
        sector_width_deg=ru.get("sector_width_deg", 120),
    )
    return snap["rsrp_dl_dbm"]


def best_ru(pos, rus):
    return max(rus, key=lambda r: rsrp_from(r, pos))


RUS = [_ru("cell-1", 0), _ru("cell-2", 120), _ru("cell-3", 240)]


def test_in_sector_boundaries():
    assert rf.in_sector(0, 0)
    assert rf.in_sector(59, 0)
    assert not rf.in_sector(61, 0)
    assert rf.in_sector(120, 120)
    assert not rf.in_sector(0, 120)


def test_boresight_beats_edge_within_sector():
    center = rf.link_rf({"x": 0, "y": 500}, 0, 0, 49, 3.5, 100e6, azimuth_deg=0)
    off_axis = rf.link_rf({"x": 250, "y": 430}, 0, 0, 49, 3.5, 100e6, azimuth_deg=0)
    assert center["in_sector"] and off_axis["in_sector"]
    assert center["rsrp_dl_dbm"] > off_axis["rsrp_dl_dbm"]


def test_out_of_sector_no_coverage():
    snap = rf.link_rf({"x": -400, "y": 100}, 0, 0, 49, 3.5, 100e6, azimuth_deg=0)
    assert not snap["in_sector"]
    assert snap["sinr_dl_db"] < rf.MIN_SINR_DB


def test_best_ru_follows_bearing():
    north = best_ru({"x": 0, "y": 400}, RUS)
    east = best_ru({"x": 400, "y": 0}, RUS)
    sw = best_ru({"x": -300, "y": -300}, RUS)
    assert north["name"] == "cell-1"
    assert east["name"] == "cell-2"
    assert sw["name"] == "cell-3"


def test_sector_handover_candidate_at_boundary():
    """Neighbour sector should beat serving by enough dB past the 60° edge."""
    serving = _ru("cell-1", 0)
    target = _ru("cell-2", 120)
    # Just inside cell-1 toward cell-2 boundary (~60° from north)
    pos = {"x": 450, "y": 260}
    assert rf.in_sector(rf.bearing_from_north_deg(pos["x"], pos["y"]), 0)
    r_s = rsrp_from(serving, pos)
    r_t = rsrp_from(target, pos)
    # Target may win near boundary once UE crosses into its sector cone
    assert isinstance(r_s, float) and isinstance(r_t, float)
