"""
LTE signalling catalog (4G: RRC + S1AP, eNB/MME/S1).
====================================================

Maps the technology-neutral flow steps to the real LTE message names found in the
TELUS decoded traces, and ships built-in default templates so the twin runs even
without a generated ``data/lte_templates.json``. Real templates (built by
scripts/build_message_templates.py) override these defaults by message name.

A future 5G NR catalog (nr.py) provides the same interface with NGAP/NR-RRC names.
"""
from __future__ import annotations

from . import procedures as proc
from .catalog import SignalingCatalog

# Common envelope constants for default/synthesized messages (cosmetic; real
# templates carry the values observed in the traces).
_PLMN = "302 221"
_ENB = "499557"


def _env(message_name, interface, protocol, decoded, **extra):
    """Build a default template: realistic envelope with per-instance tokens."""
    t = {
        "interface": interface,
        "message_name": message_name,
        "timestamp": "<<timestamp>>",
        "procedure_id": "<<procedure_id>>",
        "serving_plmn": _PLMN,
        "enb_id": _ENB,
        "cell_id": "<<cell_id>>",
        "direction": 2,
        "m_tmsi": "<<m_tmsi>>",
        "protocol": protocol,
        "decoding_status": "success",
        "decoded": decoded,
        "_version": "V17",
    }
    t.update(extra)
    return t


def _rrc(message_name, choice, body):
    return _env(message_name, "RRC", "RRC", {"message": [choice, body], "_version": "V17"})


def _s1(message_name, proc_code, msg_type, protocol_ies, **promoted):
    decoded = {
        msg_type: {"procedureCode": proc_code, "criticality": "reject",
                   "value": {"protocolIEs": protocol_ies}},
        "_version": "V17",
    }
    extra = {"mme_ue_s1ap_id": "<<mme_ue_s1ap_id>>", "enb_ue_s1ap_id": "<<enb_ue_s1ap_id>>"}
    extra.update(promoted)
    return _env(message_name, "S1", "S1AP", decoded, **extra)


class LteCatalog(SignalingCatalog):
    tech = "lte"

    MESSAGE_NAMES = {
        # uplink
        proc.RRC_CONNECTION_REQUEST: "RRC_RRC_CONNECTION_REQUEST",
        proc.RRC_CONNECTION_SETUP_COMPLETE: "RRC_RRC_CONNECTION_SETUP_COMPLETE",
        proc.RRC_SECURITY_MODE_COMPLETE: "RRC_SECURITY_MODE_COMPLETE",
        proc.RRC_UE_CAPABILITY_INFORMATION: "RRC_UE_CAPABILITY_INFORMATION",
        proc.RRC_CONNECTION_RECONFIGURATION_COMPLETE: "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE",
        proc.RRC_MEASUREMENT_REPORT: "RRC_MEASUREMENT_REPORT",
        proc.UE_DATA: "DATA",
        proc.S1_UE_CONTEXT_RELEASE_REQUEST: "S1_UE_CONTEXT_RELEASE_REQUEST",
        proc.S1_UE_CONTEXT_RELEASE_COMPLETE: "S1_UE_CONTEXT_RELEASE_COMPLETE",
        # downlink
        proc.RRC_CONNECTION_SETUP: "RRC_RRC_CONNECTION_SETUP",
        proc.RRC_SECURITY_MODE_COMMAND: "RRC_SECURITY_MODE_COMMAND",
        proc.RRC_UE_CAPABILITY_ENQUIRY: "RRC_UE_CAPABILITY_ENQUIRY",
        proc.S1_INITIAL_CONTEXT_SETUP_REQUEST: "S1_INITIAL_CONTEXT_SETUP_REQUEST",
        proc.S1_INITIAL_CONTEXT_SETUP_RESPONSE: "S1_INITIAL_CONTEXT_SETUP_RESPONSE",
        proc.RRC_CONNECTION_RECONFIGURATION: "RRC_RRC_CONNECTION_RECONFIGURATION",
        proc.RRC_CONNECTION_REJECT: "RRC_RRC_CONNECTION_REJECT",
        proc.UE_DATA_ACK: "DATA_ACK",
        proc.S1_UE_CONTEXT_RELEASE_COMMAND: "S1_UE_CONTEXT_RELEASE_COMMAND",
        proc.RRC_CONNECTION_RELEASE: "RRC_RRC_CONNECTION_RELEASE",
    }

    ENVELOPE_DEFAULTS = {"serving_plmn": _PLMN, "enb_id": _ENB, "direction": 2}

    # Canonical record_id per message type, from CallFlow/record-id-messages.txt — the
    # decoder's stable per-type id. The twin stamps these so its messages are identified
    # by record_id (the join key for matching real 22_decoded records). Types with no
    # catalog id (capability enquiry, reconfiguration, reject, user-plane DATA) -> None.
    RECORD_IDS = {
        "RRC_RRC_CONNECTION_REQUEST": 12,
        "RRC_RRC_CONNECTION_SETUP": 13,
        "RRC_RRC_CONNECTION_SETUP_COMPLETE": 23,
        "RRC_SECURITY_MODE_COMMAND": 15,
        "RRC_SECURITY_MODE_COMPLETE": 16,
        "RRC_UE_CAPABILITY_INFORMATION": 19,
        "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE": 9,
        "RRC_MEASUREMENT_REPORT": 14,
        "RRC_RRC_CONNECTION_RELEASE": 11,
        "S1_INITIAL_CONTEXT_SETUP_REQUEST": 120,
        "S1_INITIAL_CONTEXT_SETUP_RESPONSE": 121,
        "S1_UE_CONTEXT_RELEASE_REQUEST": 136,
        "S1_UE_CONTEXT_RELEASE_COMMAND": 134,
        "S1_UE_CONTEXT_RELEASE_COMPLETE": 135,
    }

    DEFAULT_TEMPLATES = {
        # ---- attach (uplink) ----
        "RRC_RRC_CONNECTION_REQUEST": _rrc(
            "RRC_RRC_CONNECTION_REQUEST", "rrcConnectionRequest",
            {"ue-Identity": ["s-TMSI", {"m-TMSI": ["<<m_tmsi>>", 32]}],
             "establishmentCause": "mo-Signalling"}),
        "RRC_RRC_CONNECTION_SETUP_COMPLETE": _rrc(
            "RRC_RRC_CONNECTION_SETUP_COMPLETE", "rrcConnectionSetupComplete",
            {"rrc-TransactionIdentifier": 0,
             "startList": [{"cn-DomainIdentity": "cs-domain", "start-Value": ["693a90", 20]}]}),
        "RRC_SECURITY_MODE_COMPLETE": _env(
            "RRC_SECURITY_MODE_COMPLETE", "RRC", "RRC",
            {"message": {"c1": {"securityModeComplete": {
                "rrc-TransactionIdentifier": 3,
                "criticalExtensions": {"securityModeComplete-r8": {}}}}},
             "_version": "V17"}),
        "RRC_UE_CAPABILITY_INFORMATION": _rrc(
            "RRC_UE_CAPABILITY_INFORMATION", "ueCapabilityInformation",
            {"rrc-TransactionIdentifier": 1, "ue-CapabilityRAT-ContainerList": []}),
        "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE": _rrc(
            "RRC_RRC_CONNECTION_RECONFIGURATION_COMPLETE", "rrcConnectionReconfigurationComplete",
            {"rrc-TransactionIdentifier": 1}),
        # ---- attach (downlink) ----
        "RRC_RRC_CONNECTION_SETUP": _rrc(
            "RRC_RRC_CONNECTION_SETUP", "rrcConnectionSetup",
            {"rrc-TransactionIdentifier": 0,
             "criticalExtensions": {"c1": {"rrcConnectionSetup-r8": {
                 "radioResourceConfigDedicated": {}}}}}),
        "RRC_SECURITY_MODE_COMMAND": _rrc(
            "RRC_SECURITY_MODE_COMMAND", "securityModeCommand",
            {"rrc-TransactionIdentifier": 2,
             "criticalExtensions": {"c1": {"securityModeCommand-r8": {
                 "securityConfigSMC": {"securityAlgorithmConfig": {
                     "cipheringAlgorithm": "eea0", "integrityProtAlgorithm": "eia2"}}}}}}),
        "RRC_UE_CAPABILITY_ENQUIRY": _rrc(
            "RRC_UE_CAPABILITY_ENQUIRY", "ueCapabilityEnquiry",
            {"rrc-TransactionIdentifier": 1,
             "criticalExtensions": {"c1": {"ueCapabilityEnquiry-r8": {
                 "ue-CapabilityRequest": ["eutra"]}}}}),
        "S1_INITIAL_CONTEXT_SETUP_REQUEST": _s1(
            "S1_INITIAL_CONTEXT_SETUP_REQUEST", 9, "initiatingMessage",
            [{"id": 0, "criticality": "reject", "value": "<<mme_ue_s1ap_id>>"},
             {"id": 8, "criticality": "reject", "value": "<<enb_ue_s1ap_id>>"},
             {"id": 24, "criticality": "reject", "value": [{"id": 52, "criticality": "reject", "value": {
                 "e-RAB-ID": 5,
                 "e-RABlevelQoSParameters": {"qCI": 9, "allocationRetentionPriority": {
                     "priorityLevel": 10,
                     "pre-emptionCapability": "shall-not-trigger-pre-emption",
                     "pre-emptionVulnerability": "pre-emptable"}},
                 "gTP-TEID": "57b88914"}}]}],
            erab_to_be_setup_count=1),
        "S1_INITIAL_CONTEXT_SETUP_RESPONSE": _s1(
            "S1_INITIAL_CONTEXT_SETUP_RESPONSE", 9, "successfulOutcome",
            [{"id": 0, "criticality": "ignore", "value": "<<mme_ue_s1ap_id>>"},
             {"id": 8, "criticality": "ignore", "value": "<<enb_ue_s1ap_id>>"},
             {"id": 51, "criticality": "ignore", "value": [{"id": 50, "criticality": "ignore", "value": {
                 "e-RAB-ID": 5, "gTP-TEID": "0300090d"}}]}],
            erab_setup_count=1),
        # ---- steady state ----
        "RRC_MEASUREMENT_REPORT": _rrc(
            "RRC_MEASUREMENT_REPORT", "measurementReport",
            {"measurementIdentity": 9, "v390nonCriticalExtensions": {"measurementReport-v390ext": {}}}),
        "RRC_RRC_CONNECTION_RECONFIGURATION": _rrc(
            "RRC_RRC_CONNECTION_RECONFIGURATION", "rrcConnectionReconfiguration",
            {"rrc-TransactionIdentifier": 1,
             "criticalExtensions": {"c1": {"rrcConnectionReconfiguration-r8": {
                 "radioResourceConfigDedicated": {}}}}}),
        "RRC_RRC_CONNECTION_REJECT": _rrc(
            "RRC_RRC_CONNECTION_REJECT", "rrcConnectionReject",
            {"criticalExtensions": {"c1": {"rrcConnectionReject-r8": {"waitTime": 16}}}}),
        # DATA / DATA_ACK are twin user-plane stand-ins (no LTE control-plane analogue).
        "DATA": _env("DATA", "TWIN", "TWIN", {"message": "userPlaneData"}),
        "DATA_ACK": _env("DATA_ACK", "TWIN", "TWIN", {"message": "userPlaneDataAck"}),
        # ---- release ----
        "S1_UE_CONTEXT_RELEASE_REQUEST": _s1(
            "S1_UE_CONTEXT_RELEASE_REQUEST", 18, "initiatingMessage",
            [{"id": 0, "criticality": "reject", "value": "<<mme_ue_s1ap_id>>"},
             {"id": 8, "criticality": "reject", "value": "<<enb_ue_s1ap_id>>"},
             {"id": 2, "criticality": "ignore", "value": {"choice": "radioNetwork", "value": "user-inactivity"}}],
            cause={"type": "radioNetwork", "value": "user-inactivity", "label": "user-inactivity"}),
        "S1_UE_CONTEXT_RELEASE_COMMAND": _s1(
            "S1_UE_CONTEXT_RELEASE_COMMAND", 23, "initiatingMessage",
            [{"id": 99, "criticality": "reject", "value": {"choice": "uE-S1AP-ID-pair", "value": {
                "mME-UE-S1AP-ID": "<<mme_ue_s1ap_id>>", "eNB-UE-S1AP-ID": "<<enb_ue_s1ap_id>>"}}},
             {"id": 2, "criticality": "ignore", "value": {"choice": "nas", "value": "detach"}}],
            cause={"type": "nas", "value": "detach", "label": "detach"}),
        "S1_UE_CONTEXT_RELEASE_COMPLETE": _s1(
            "S1_UE_CONTEXT_RELEASE_COMPLETE", 23, "successfulOutcome",
            [{"id": 0, "criticality": "ignore", "value": "<<mme_ue_s1ap_id>>"},
             {"id": 8, "criticality": "ignore", "value": "<<enb_ue_s1ap_id>>"}]),
        "RRC_RRC_CONNECTION_RELEASE": _rrc(
            "RRC_RRC_CONNECTION_RELEASE", "rrcConnectionRelease",
            ["r3", {"rrcConnectionRelease-r3": {
                "rrc-TransactionIdentifier": 2, "releaseCause": "normalRelease"}}]),
    }
