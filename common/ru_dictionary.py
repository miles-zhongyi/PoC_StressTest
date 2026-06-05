"""
Per-RU state dictionary: static site/cell config + dynamically updated UE assignments.

Each RU container maintains one JSON document (default ``data/ru_state/{SITE_ID}.json``)
with bandwidth, RU type, per-cell PRB pools, and per-UE frequency band + PRB grant.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock


def nr_band_from_freq_ghz(freq_ghz: float) -> str:
    """Map carrier frequency to a 5G NR band label (twin cosmetic / observability)."""
    g = float(freq_ghz)
    if 3.3 <= g <= 4.0:
        return "n78"
    if 2.5 <= g <= 2.7:
        return "n41"
    if 1.8 <= g <= 2.2:
        return "n25"
    if 0.6 <= g <= 0.9:
        return "n71"
    return f"n-custom-{g:.3f}GHz"


class RuDictionary:
    """In-memory RU registry mirrored to a JSON file for external tools."""

    def __init__(
        self,
        *,
        site_id: str,
        ru_type: str,
        bandwidth_mhz: float,
        default_freq_ghz: float,
        position: dict,
        sectors: dict[str, dict],
        total_prbs_per_cell: int,
        scs_khz: int = 30,
    ):
        self.site_id = site_id
        self.ru_type = ru_type
        self.bandwidth_mhz = float(bandwidth_mhz)
        self.default_freq_ghz = float(default_freq_ghz)
        self.position = {"x": float(position["x"]), "y": float(position["y"])}
        self.total_prbs_per_cell = int(total_prbs_per_cell)
        self.scs_khz = int(scs_khz)
        self.sectors = {
            cell_id: {
                "azimuth_deg": cfg.get("azimuth_deg"),
                "sector_width_deg": float(cfg.get("sector_width_deg", 120)),
                "freq_ghz": float(cfg.get("freq_ghz", default_freq_ghz)),
                "bandwidth_mhz": float(cfg.get("bandwidth_mhz", bandwidth_mhz)),
                "total_prbs": int(cfg.get("total_prbs", total_prbs_per_cell)),
            }
            for cell_id, cfg in sectors.items()
        }
        self._ues: dict[str, dict] = {}
        self._lock = Lock()
        self.updated_ts = time.time()

    @property
    def num_cells(self) -> int:
        return len(self.sectors)

    def _cell_freq(self, cell_id: str) -> tuple[float, str]:
        cfg = self.sectors.get(cell_id, {})
        freq = float(cfg.get("freq_ghz", self.default_freq_ghz))
        return freq, nr_band_from_freq_ghz(freq)

    def note_uplink(self, ue_id: str, cell_id: str, uplink_twin: dict | None = None):
        """UE sent an uplink on this RU (serving cell may change before the DU reply)."""
        if not ue_id or cell_id not in self.sectors:
            return
        freq, band = self._cell_freq(cell_id)
        rf = (uplink_twin or {}).get("rf") or {}
        with self._lock:
            prev = self._ues.get(ue_id, {})
            self._ues[ue_id] = {
                "ue_id": ue_id,
                "cell": cell_id,
                "frequency_ghz": freq,
                "frequency_band": band,
                "allocated_prbs": prev.get("allocated_prbs"),
                "mcs": prev.get("mcs"),
                "rsrp_dbm": rf.get("rsrp_dl_dbm"),
                "sinr_dl_db": rf.get("sinr_dl_db"),
                "updated_ts": time.time(),
            }
            self.updated_ts = time.time()

    def note_downlink(self, ue_id: str, cell_id: str, reply_twin: dict | None = None):
        """Apply DU reply fields (PRB grant, reject clears admission fields)."""
        if not ue_id:
            return
        tw = reply_twin or {}
        with self._lock:
            if ue_id not in self._ues and cell_id in self.sectors:
                freq, band = self._cell_freq(cell_id)
                self._ues[ue_id] = {
                    "ue_id": ue_id,
                    "cell": cell_id,
                    "frequency_ghz": freq,
                    "frequency_band": band,
                    "updated_ts": time.time(),
                }
            if ue_id not in self._ues:
                return
            row = self._ues[ue_id]
            if cell_id in self.sectors:
                row["cell"] = cell_id
                freq, band = self._cell_freq(cell_id)
                row["frequency_ghz"] = freq
                row["frequency_band"] = band
            if tw.get("cause") and not tw.get("allocated_prbs"):
                row["allocated_prbs"] = 0
                row["mcs"] = None
            else:
                if "allocated_prbs" in tw:
                    row["allocated_prbs"] = tw["allocated_prbs"]
                if "mcs" in tw:
                    row["mcs"] = tw["mcs"]
            row["updated_ts"] = time.time()
            self.updated_ts = time.time()

    def remove_ue(self, ue_id: str):
        if not ue_id:
            return
        with self._lock:
            if self._ues.pop(ue_id, None) is not None:
                self.updated_ts = time.time()

    def _cell_usage(self) -> dict[str, dict]:
        usage = {
            cid: {
                "connected_ues": 0,
                "used_prbs": 0,
                "free_prbs": cfg["total_prbs"],
            }
            for cid, cfg in self.sectors.items()
        }
        for ue in self._ues.values():
            cid = ue.get("cell")
            prbs = ue.get("allocated_prbs") or 0
            if cid not in usage or not prbs:
                continue
            usage[cid]["connected_ues"] += 1
            usage[cid]["used_prbs"] += int(prbs)
        for cid, u in usage.items():
            total = self.sectors[cid]["total_prbs"]
            u["total_prbs"] = total
            u["free_prbs"] = max(0, total - u["used_prbs"])
            u["azimuth_deg"] = self.sectors[cid]["azimuth_deg"]
            u["sector_width_deg"] = self.sectors[cid]["sector_width_deg"]
            u["freq_ghz"] = self.sectors[cid]["freq_ghz"]
            u["frequency_band"] = nr_band_from_freq_ghz(self.sectors[cid]["freq_ghz"])
            u["bandwidth_mhz"] = self.sectors[cid]["bandwidth_mhz"]
        return usage

    def to_dict(self) -> dict:
        with self._lock:
            ues = {uid: dict(row) for uid, row in self._ues.items()}
        cells = self._cell_usage()
        return {
            "site_id": self.site_id,
            "ru_type": self.ru_type,
            "updated_ts": self.updated_ts,
            "bandwidth_mhz": self.bandwidth_mhz,
            "freq_ghz": self.default_freq_ghz,
            "frequency_band": nr_band_from_freq_ghz(self.default_freq_ghz),
            "scs_khz": self.scs_khz,
            "num_cells": self.num_cells,
            "position": dict(self.position),
            "cells": cells,
            "ues": ues,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.to_dict(), indent=2)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)

    @classmethod
    def from_env(cls) -> RuDictionary:
        site_id = os.environ.get("SITE_ID", os.environ.get("CELL_ID", "RU1"))
        ru_type = os.environ.get("RU_TYPE", "macro-3-sector-O-RU")
        bandwidth_mhz = float(os.environ.get("BANDWIDTH_MHZ", "100"))
        freq_ghz = float(os.environ.get("FREQ_GHZ", "3.5"))
        total_prbs = int(os.environ.get("CELL_TOTAL_PRBS", os.environ.get("TOTAL_PRBS", "250")))
        scs_khz = int(os.environ.get("SCS_KHZ", "30"))
        position = {
            "x": float(os.environ.get("RU_X", "0")),
            "y": float(os.environ.get("RU_Y", "0")),
        }
        sectors = parse_sectors_env(
            bandwidth_mhz=bandwidth_mhz,
            default_freq_ghz=freq_ghz,
            total_prbs=total_prbs,
        )
        return cls(
            site_id=site_id,
            ru_type=ru_type,
            bandwidth_mhz=bandwidth_mhz,
            default_freq_ghz=freq_ghz,
            position=position,
            sectors=sectors,
            total_prbs_per_cell=total_prbs,
            scs_khz=scs_khz,
        )


def parse_sectors_env(
    *,
    bandwidth_mhz: float,
    default_freq_ghz: float,
    total_prbs: int,
) -> dict[str, dict]:
    """Build sector cell config from ``SECTORS`` JSON or single ``CELL_ID`` fallback."""
    cell_id = os.environ.get("CELL_ID", "cell-1")
    az_raw = os.environ.get("RU_AZIMUTH_DEG", "").strip()
    azimuth = float(az_raw) if az_raw else None
    raw = os.environ.get("SECTORS", "").strip()
    if raw:
        out = {}
        for s in json.loads(raw):
            cid = s["cell"]
            az = s.get("azimuth", s.get("azimuth_deg"))
            out[cid] = {
                "azimuth_deg": float(az) if az is not None else None,
                "sector_width_deg": float(s.get("sector_width_deg", 120)),
                "freq_ghz": float(s.get("freq_ghz", default_freq_ghz)),
                "bandwidth_mhz": float(s.get("bandwidth_mhz", bandwidth_mhz)),
                "total_prbs": int(s.get("total_prbs", total_prbs)),
            }
        return out
    return {
        cell_id: {
            "azimuth_deg": azimuth,
            "sector_width_deg": float(os.environ.get("SECTOR_WIDTH_DEG", "120")),
            "freq_ghz": default_freq_ghz,
            "bandwidth_mhz": bandwidth_mhz,
            "total_prbs": total_prbs,
        }
    }
