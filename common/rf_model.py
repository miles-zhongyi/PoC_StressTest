"""
Simplified-but-coherent RF model used by the RU to turn UE geometry into radio
conditions, and by the DU to turn radio conditions into a PRB requirement.

The chain is:

    distance + tx power  --(path loss)-->  RSRP / SINR
    SINR                 --(Shannon)----->  spectral efficiency (bits/s/Hz)
    SE + traffic demand  --------------->   number of PRBs needed

Traffic profiles:
  * voip  — VoLTE-style voice (~12–48 kbps). Admission reserves 1–2 PRBs per
            session (typical instant scheduling is ~1–2 PRBs per slot; the DU
            models a sustained pool reservation capped at VOIP_MAX_PRBS).
  * data  — broadband-style demand (Mbps), for capacity stress without a cap.

It is intentionally a single-layer (no MIMO) model. Numbers land in realistic
ranges for an n78 (3.5 GHz) 100 MHz macro cell, which is enough to make the
capacity behaviour believable: near UEs are cheap, cell-edge UEs are expensive,
and beyond ~1.3 km the UE falls out of coverage and is rejected.
"""
import math

_C = 299_792_458.0  # speed of light, m/s

# 256-QAM, code rate ~0.93 -> ~7.4 bits/s/Hz is the practical single-layer ceiling
MAX_SE = 7.4063
# Below roughly QPSK 1/8 the block error rate explodes -> treat as no coverage
MIN_SINR_DB = -6.7
# Shannon is optimistic; real schedulers reach ~0.6 of it after coding overhead
IMPL_EFFICIENCY = 0.6
# Resource elements lost to CP, DMRS, control channels, etc.
RE_OVERHEAD = 0.75


def _to_lin(db):
    return 10.0 ** (db / 10.0)


def _to_db(lin):
    return 10.0 * math.log10(lin) if lin > 0 else -math.inf


def path_loss_db(distance_m, freq_ghz, exponent=3.5, d0=1.0):
    """Log-distance path loss anchored to free-space loss at d0 = 1 m."""
    d = max(distance_m, d0)
    f_hz = freq_ghz * 1e9
    pl_d0 = 20.0 * math.log10(4.0 * math.pi * d0 * f_hz / _C)  # FSPL at 1 m
    return pl_d0 + 10.0 * exponent * math.log10(d / d0)


def noise_floor_dbm(bandwidth_hz, noise_figure_db=7.0):
    """Thermal noise over the channel: -174 dBm/Hz + 10log10(BW) + NF."""
    return -174.0 + 10.0 * math.log10(bandwidth_hz) + noise_figure_db


def rsrp_dbm(tx_power_dbm, distance_m, freq_ghz, tx_gain_db=15.0, n_subcarriers=3276):
    """RSRP is per-subcarrier (per-RE) power, so total power is spread over all SCs."""
    pl = path_loss_db(distance_m, freq_ghz)
    return tx_power_dbm - 10.0 * math.log10(n_subcarriers) + tx_gain_db - pl


def sinr_db(
    tx_power_dbm,
    distance_m,
    freq_ghz,
    bandwidth_hz,
    tx_gain_db=15.0,
    rx_gain_db=0.0,
    interference_margin_db=3.0,
    noise_figure_db=7.0,
):
    """Wideband SINR = received signal over (thermal noise + neighbour interference)."""
    pl = path_loss_db(distance_m, freq_ghz)
    rx_signal_dbm = tx_power_dbm + tx_gain_db + rx_gain_db - pl
    n_dbm = noise_floor_dbm(bandwidth_hz, noise_figure_db)
    interf_dbm = n_dbm + interference_margin_db
    noise_plus_interf = _to_db(_to_lin(n_dbm) + _to_lin(interf_dbm))
    return rx_signal_dbm - noise_plus_interf


def spectral_efficiency(sinr):
    """SINR (dB) -> achievable spectral efficiency (bits/s/Hz), 0 if out of coverage."""
    if sinr < MIN_SINR_DB:
        return 0.0
    se = IMPL_EFFICIENCY * math.log2(1.0 + _to_lin(sinr))
    return min(se, MAX_SE)


def prb_bandwidth_hz(scs_khz):
    """One PRB = 12 subcarriers."""
    return 12.0 * scs_khz * 1000.0


def throughput_per_prb_mbps(sinr, scs_khz=30):
    se = spectral_efficiency(sinr)
    return se * prb_bandwidth_hz(scs_khz) * RE_OVERHEAD / 1e6


# VoIP: EVS/WB-AMR class codecs ~12–24 kb/s + overhead; reserve ≤2 PRBs (per-slot
# scheduling in live networks is often 1–2 PRBs; the DU holds a small sustained grant).
VOIP_MAX_PRBS = 2
VOIP_MIN_PRBS = 1


def prbs_for_demand(demand_mbps, sinr, scs_khz=30, max_prbs=None, min_prbs=1):
    """
    How many PRBs this UE needs to meet `demand_mbps` at the given SINR.

    Returns (required_prbs, per_prb_mbps, spectral_efficiency).
    required_prbs is None when the UE cannot be served at all (no coverage).
  Optional max_prbs caps the grant (VoIP uses 1–2).
    """
    se = spectral_efficiency(sinr)
    if se <= 0.0:
        return None, 0.0, 0.0
    per_prb = se * prb_bandwidth_hz(scs_khz) * RE_OVERHEAD / 1e6
    required = max(min_prbs, math.ceil(demand_mbps / per_prb))
    if max_prbs is not None:
        required = min(required, max_prbs)
    return required, per_prb, se


def prbs_for_voip(sinr, scs_khz=30):
    """
    VoLTE-style voice: reserve exactly 1 PRB (good RF) or 2 PRBs (marginal RF).

    Not derived from demand_mbps — voice uses ~12–48 kb/s but only 1–2 PRBs are
    scheduled per slot in live networks; the DU models a small sustained grant.
    """
    se = spectral_efficiency(sinr)
    if se <= 0.0:
        return None, 0.0, 0.0
    per_prb = se * prb_bandwidth_hz(scs_khz) * RE_OVERHEAD / 1e6
    # Marginal link: 2 PRBs; otherwise 1 (matches field reports of 1–2 PRBs/slot)
    required = VOIP_MAX_PRBS if sinr < 5.0 else VOIP_MIN_PRBS
    return required, per_prb, se


def prbs_for_traffic(demand_mbps, sinr, scs_khz=30, profile="voip"):
    """Admission grant sized by traffic profile (voip | data)."""
    p = (profile or "voip").lower()
    if p == "data":
        return prbs_for_demand(demand_mbps, sinr, scs_khz, max_prbs=None)
    return prbs_for_voip(sinr, scs_khz)


def mcs_from_se(se):
    """Rough mapping of spectral efficiency onto an MCS index (0..27) for display."""
    return max(0, min(27, round(se / MAX_SE * 27)))


# ---- sector antenna (macro eNB: 3 × 120°) ---------------------------------
SECTOR_WIDTH_DEG = 120.0
SECTOR_EDGE_LOSS_DB = 18.0   # rolloff at ±60° from boresight
NO_COVERAGE_GAIN_DB = -999.0


def bearing_from_north_deg(dx, dy):
    """Azimuth from site to UE: 0° = north (+y), 90° = east (+x), clockwise."""
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def angular_diff_deg(bearing_deg, azimuth_deg):
    """Signed shortest angle between bearing and sector boresight (-180..180)."""
    return (bearing_deg - azimuth_deg + 180.0) % 360.0 - 180.0


def in_sector(bearing_deg, azimuth_deg, sector_width_deg=SECTOR_WIDTH_DEG):
    return abs(angular_diff_deg(bearing_deg, azimuth_deg)) <= sector_width_deg / 2.0


def sector_antenna_gain_db(
    bearing_deg,
    azimuth_deg,
    sector_width_deg=SECTOR_WIDTH_DEG,
    edge_loss_db=SECTOR_EDGE_LOSS_DB,
):
    """Antenna gain relative to boresight; NO_COVERAGE_GAIN_DB outside the sector."""
    half = sector_width_deg / 2.0
    diff = abs(angular_diff_deg(bearing_deg, azimuth_deg))
    if diff > half:
        return NO_COVERAGE_GAIN_DB
    t = diff / half if half > 0 else 0.0
    return -edge_loss_db * (t * t)


def link_rf(
    ue_pos,
    site_x,
    site_y,
    tx_power_dbm,
    freq_ghz,
    bandwidth_hz,
    *,
    tx_gain_db=15.0,
    ue_tx_power_dbm=23.0,
    azimuth_deg=None,
    sector_width_deg=SECTOR_WIDTH_DEG,
):
    """
    DL/UL RF snapshot for a UE relative to one RU site.

    When `azimuth_deg` is set the link uses a 120° sector pattern; otherwise
    omnidirectional (legacy single-RU setups).
    """
    dx = ue_pos["x"] - site_x
    dy = ue_pos["y"] - site_y
    distance = max(1.0, math.hypot(dx, dy))
    bearing = bearing_from_north_deg(dx, dy)
    ant_extra = 0.0
    in_cov = True
    if azimuth_deg is not None:
        ant_extra = sector_antenna_gain_db(bearing, azimuth_deg, sector_width_deg)
        if ant_extra <= NO_COVERAGE_GAIN_DB / 2:
            in_cov = False
            return {
                "distance_m": round(distance, 1),
                "bearing_deg": round(bearing, 1),
                "in_sector": False,
                "rsrp_dl_dbm": -140.0,
                "sinr_dl_db": MIN_SINR_DB - 10.0,
                "sinr_ul_db": MIN_SINR_DB - 10.0,
            }
    rsrp = rsrp_dbm(tx_power_dbm, distance, freq_ghz, tx_gain_db) + ant_extra
    sinr_dl = sinr_db(tx_power_dbm, distance, freq_ghz, bandwidth_hz, tx_gain_db=tx_gain_db) + ant_extra
    sinr_ul = sinr_db(
        ue_tx_power_dbm, distance, freq_ghz, bandwidth_hz,
        tx_gain_db=0.0, rx_gain_db=tx_gain_db,
    ) + ant_extra
    return {
        "distance_m": round(distance, 1),
        "bearing_deg": round(bearing, 1),
        "in_sector": in_cov,
        "sector_gain_db": round(ant_extra, 2),
        "rsrp_dl_dbm": round(rsrp, 1),
        "sinr_dl_db": round(sinr_dl, 1),
        "sinr_ul_db": round(sinr_ul, 1),
    }
