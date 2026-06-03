"""
Technology-neutral signalling flow definitions.
===============================================

The twin's link is a strict one-uplink -> one-reply exchange proxied by the RU. To
model a fuller call flow without breaking that, a procedure is expressed as an
ordered list of `Step`s: each UE *uplink* message elicits exactly one network
*downlink* reply, and `action` tells the DU what capacity-affecting work (if any) to
do at that step. The UE walks the list; the DU classifies each uplink back to a step
and replies with the mapped downlink.

`uplink`/`downlink` here are *logical* names. A per-technology catalog (lte.py,
future nr.py) maps each to a concrete real message name + template.
"""
from __future__ import annotations

from dataclasses import dataclass

# Capacity actions the DU performs when it handles a step's uplink.
ACT_NONE = "none"
ACT_ADMIT = "admit"        # run admission control, allocate PRBs (may reject)
ACT_RECONFIG = "reconfig"  # re-evaluate grant after mobility (may drop)
ACT_RELEASE = "release"    # reclaim PRBs


@dataclass(frozen=True)
class Step:
    name: str          # logical step id (stable, used across the codebase)
    uplink: str        # logical name of the UE->network message
    downlink: str      # logical name of the network->UE reply
    action: str = ACT_NONE


# ---- logical message names (mapped to concrete messages by the catalog) -----
# Uplink (UE -> network)
RRC_CONNECTION_REQUEST = "RRC_CONNECTION_REQUEST"
RRC_CONNECTION_SETUP_COMPLETE = "RRC_CONNECTION_SETUP_COMPLETE"
RRC_SECURITY_MODE_COMPLETE = "RRC_SECURITY_MODE_COMPLETE"
RRC_UE_CAPABILITY_INFORMATION = "RRC_UE_CAPABILITY_INFORMATION"
RRC_CONNECTION_RECONFIGURATION_COMPLETE = "RRC_CONNECTION_RECONFIGURATION_COMPLETE"
RRC_MEASUREMENT_REPORT = "RRC_MEASUREMENT_REPORT"
UE_DATA = "DATA"
S1_UE_CONTEXT_RELEASE_REQUEST = "S1_UE_CONTEXT_RELEASE_REQUEST"
S1_UE_CONTEXT_RELEASE_COMPLETE = "S1_UE_CONTEXT_RELEASE_COMPLETE"

# Downlink (network -> UE)
RRC_CONNECTION_SETUP = "RRC_CONNECTION_SETUP"
RRC_SECURITY_MODE_COMMAND = "RRC_SECURITY_MODE_COMMAND"
RRC_UE_CAPABILITY_ENQUIRY = "RRC_UE_CAPABILITY_ENQUIRY"
S1_INITIAL_CONTEXT_SETUP_REQUEST = "S1_INITIAL_CONTEXT_SETUP_REQUEST"
S1_INITIAL_CONTEXT_SETUP_RESPONSE = "S1_INITIAL_CONTEXT_SETUP_RESPONSE"
RRC_CONNECTION_RECONFIGURATION = "RRC_CONNECTION_RECONFIGURATION"
RRC_CONNECTION_REJECT = "RRC_CONNECTION_REJECT"
UE_DATA_ACK = "DATA_ACK"
S1_UE_CONTEXT_RELEASE_COMMAND = "S1_UE_CONTEXT_RELEASE_COMMAND"
RRC_CONNECTION_RELEASE = "RRC_CONNECTION_RELEASE"

# ---- step ids ---------------------------------------------------------------
STEP_RRC_SETUP = "rrc_setup"
STEP_SETUP_COMPLETE = "setup_complete"
STEP_SECURITY = "security"
STEP_CAPABILITY = "capability"
STEP_CONTEXT_SETUP = "context_setup"   # admission happens here
STEP_RECONFIG_COMPLETE = "reconfig_complete"
STEP_MEASUREMENT = "measurement"
STEP_DATA = "data"
STEP_RELEASE_REQUEST = "release_request"  # PRBs reclaimed here
STEP_RELEASE_COMPLETE = "release_complete"

# Attach: each UE uplink draws the next network downlink. Admission (PRB grant) is
# performed when the UE's capability info arrives and the network would issue the
# Initial Context Setup Request; rejection there ends the attach.
ATTACH_FLOW: tuple[Step, ...] = (
    Step(STEP_RRC_SETUP, RRC_CONNECTION_REQUEST, RRC_CONNECTION_SETUP),
    Step(STEP_SETUP_COMPLETE, RRC_CONNECTION_SETUP_COMPLETE, RRC_SECURITY_MODE_COMMAND),
    Step(STEP_SECURITY, RRC_SECURITY_MODE_COMPLETE, RRC_UE_CAPABILITY_ENQUIRY),
    Step(STEP_CONTEXT_SETUP, RRC_UE_CAPABILITY_INFORMATION, S1_INITIAL_CONTEXT_SETUP_REQUEST, ACT_ADMIT),
    Step(STEP_RECONFIG_COMPLETE, RRC_CONNECTION_RECONFIGURATION_COMPLETE, S1_INITIAL_CONTEXT_SETUP_RESPONSE),
)

# Steady-state exchanges (repeat for the life of the session).
MEASUREMENT_STEP = Step(STEP_MEASUREMENT, RRC_MEASUREMENT_REPORT, RRC_CONNECTION_RECONFIGURATION, ACT_RECONFIG)
DATA_STEP = Step(STEP_DATA, UE_DATA, UE_DATA_ACK)

# Release: request reclaims PRBs; complete is the final uplink and ends the session.
RELEASE_FLOW: tuple[Step, ...] = (
    Step(STEP_RELEASE_REQUEST, S1_UE_CONTEXT_RELEASE_REQUEST, S1_UE_CONTEXT_RELEASE_COMMAND, ACT_RELEASE),
    Step(STEP_RELEASE_COMPLETE, S1_UE_CONTEXT_RELEASE_COMPLETE, RRC_CONNECTION_RELEASE),
)

ALL_STEPS: tuple[Step, ...] = ATTACH_FLOW + (MEASUREMENT_STEP, DATA_STEP) + RELEASE_FLOW

# uplink logical-name -> Step, for the DU to classify what it receives.
STEP_BY_UPLINK: dict[str, Step] = {s.uplink: s for s in ALL_STEPS}

# The final uplink of the session (UE has nothing more to send afterwards).
FINAL_UPLINK = S1_UE_CONTEXT_RELEASE_COMPLETE
