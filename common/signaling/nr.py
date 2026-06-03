"""
5G NR signalling catalog — reserved for future work.
=====================================================

TELUS runs both 4G LTE and 5G SA. The LTE catalog (lte.py) is implemented from the
decoded S1/RRC traces. The 5G flow (NR-RRC over the Uu, NGAP toward the AMF: RRC
Setup, Registration Request, Authentication, Security Mode, UE Capability, Initial
Context Setup, PDU Session Setup) follows the same Step-based structure and will plug
in here once decoded NR/NGAP traces are available — see CallFlow/ for the reference
sequence.
"""
from __future__ import annotations

from .catalog import SignalingCatalog


class NrCatalog(SignalingCatalog):
    tech = "nr"

    def __init__(self, templates_path=None):
        raise NotImplementedError(
            "5G NR signalling catalog is not implemented yet. "
            "Set RADIO_TECH=lte (default) to use the 4G LTE flow."
        )
