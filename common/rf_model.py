"""
Simplified-but-coherent RF model used by the RU to turn UE geometry into radio
conditions, and by the DU to turn radio conditions into a PRB requirement.

The chain is:

    distance + tx power  --(path loss)-->  RSRP / SINR
    SINR                 --(Shannon)----->  spectral efficiency (bits/s/Hz)
    SE + traffic demand  --------------->   number of PRBs needed

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


def prbs_for_demand(demand_mbps, sinr, scs_khz=30):
    """
    How many PRBs this UE needs to meet `demand_mbps` at the given SINR.

    Returns (required_prbs, per_prb_mbps, spectral_efficiency).
    required_prbs is None when the UE cannot be served at all (no coverage).
    """
    se = spectral_efficiency(sinr)
    if se <= 0.0:
        return None, 0.0, 0.0
    per_prb = se * prb_bandwidth_hz(scs_khz) * RE_OVERHEAD / 1e6
    required = max(1, math.ceil(demand_mbps / per_prb))
    return required, per_prb, se


def mcs_from_se(se):
    """Rough mapping of spectral efficiency onto an MCS index (0..27) for display."""
    return max(0, min(27, round(se / MAX_SE * 27)))
