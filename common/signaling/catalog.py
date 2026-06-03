"""
SignalingCatalog — turns logical flow steps into realistic messages and back.
=============================================================================

A catalog binds the technology-neutral flow in procedures.py to concrete, real
message names + templates for one radio technology. It does two things:

  * build(logical_name, ...) -> a wire message that carries a realistic envelope and
    ``decoded`` body (from a template) merged with a ``_twin`` sidecar holding the
    simulation-only fields real traces lack (ue id, demand, rf, allocated prbs ...).
  * classify(msg) -> the flow Step a received uplink corresponds to, so the DU knows
    which capacity action to run and which downlink to reply with.

The on-wire message keeps ``txn`` at top level for the RU's request/reply
correlation; all twin routing/functional state lives under ``_twin`` so it never
collides with the realistic (and merely cosmetic) envelope fields like numeric
``cell_id``.
"""
from __future__ import annotations

import itertools
import time
import zlib
from datetime import datetime, timezone

from . import procedures as proc
from .templates import fill, load_templates


def _stable_int(text: str, mod: int) -> int:
    """Deterministic small int from a string (stable across processes, unlike hash())."""
    return zlib.crc32(text.encode("utf-8")) % mod


class SignalingCatalog:
    tech = "base"

    # Subclasses provide these:
    MESSAGE_NAMES: dict[str, str] = {}        # logical name -> real message_name
    DEFAULT_TEMPLATES: dict[str, dict] = {}   # real message_name -> built-in template
    ENVELOPE_DEFAULTS: dict = {}              # serving_plmn / enb_id for synthesized msgs
    RECORD_IDS: dict[str, int] = {}           # real message_name -> canonical record_id

    def __init__(self, templates_path=None):
        # Real templates (from the traces) override the built-in defaults by name.
        self.templates = {**self.DEFAULT_TEMPLATES, **load_templates(templates_path)}
        self._proc_ids = itertools.count(100000)
        self._rec_ids = itertools.count(1)
        # real uplink message_name -> Step, for classify()
        self._step_by_real_uplink = {
            self.MESSAGE_NAMES.get(s.uplink, s.uplink): s for s in proc.ALL_STEPS
        }
        self._step_by_id = {s.name: s for s in proc.ALL_STEPS}

    # ---- introspection used by the UE driver / DU --------------------------
    def attach_flow(self):
        return proc.ATTACH_FLOW

    def release_flow(self):
        return proc.RELEASE_FLOW

    @property
    def measurement_step(self):
        return proc.MEASUREMENT_STEP

    @property
    def data_step(self):
        return proc.DATA_STEP

    def real_name(self, logical: str) -> str:
        return self.MESSAGE_NAMES.get(logical, logical)

    def is_final_uplink(self, msg: dict) -> bool:
        return msg.get("message_name") == self.MESSAGE_NAMES.get(proc.FINAL_UPLINK)

    def describe(self) -> dict:
        """Structured description of the call flow this catalog actually implements —
        the real per-step uplink/downlink message names, their interface, and the DU
        capacity action. Generated from procedures.py + the name mapping, so it never
        drifts from the code. Used by the dashboard's call-flow view."""
        def leg(logical):
            name = self.real_name(logical)
            return {"name": name, "interface": self._iface_for(name)[0]}

        def step(s):
            return {"step": s.name, "action": s.action,
                    "uplink": leg(s.uplink), "downlink": leg(s.downlink)}

        return {
            "tech": self.tech,
            "phases": [
                {"name": "Attach / RRC connection setup",
                 "steps": [step(s) for s in proc.ATTACH_FLOW]},
                {"name": "Steady state (repeats while connected)",
                 "steps": [step(proc.MEASUREMENT_STEP), step(proc.DATA_STEP)]},
                {"name": "Release",
                 "steps": [step(s) for s in proc.RELEASE_FLOW]},
            ],
        }

    # ---- classification (DU side) ------------------------------------------
    def classify(self, msg: dict):
        """Return the flow Step for a received uplink, or None if unrecognised."""
        name = msg.get("message_name")
        step = self._step_by_real_uplink.get(name)
        if step is not None:
            return step
        # Fall back to the twin sidecar, then the legacy `type` field.
        twin = msg.get("_twin") or {}
        if twin.get("step") in self._step_by_id:
            return self._step_by_id[twin["step"]]
        return None

    def record_id_for(self, real_name: str):
        """Canonical record_id for a message type (from record-id-messages.txt), or None."""
        return self.RECORD_IDS.get(real_name)

    # ---- message construction (both sides) ---------------------------------
    def _token_context(self, ue_id: str, cell: str, real_name: str) -> dict:
        # record_id is the canonical per-type id (the wire identifier the twin uses);
        # fall back to a synthetic counter only for types with no catalog id.
        rid = self.record_id_for(real_name)
        return {
            "record_id": rid if rid is not None else next(self._rec_ids),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "procedure_id": next(self._proc_ids),
            "cell_id": self.cell_num(cell),
            "m_tmsi": _stable_int(ue_id, 1_000_000),
            "enb_ue_s1ap_id": _stable_int(ue_id, 16_777_215),
            "mme_ue_s1ap_id": _stable_int("mme:" + ue_id, 4_294_967_295),
        }

    def cell_num(self, cell: str) -> int:
        """Cosmetic numeric cell id for the realistic envelope (e.g. cell-1 -> 1)."""
        if cell:
            tail = cell.rsplit("-", 1)[-1]
            if tail.isdigit():
                return int(tail)
            return _stable_int(cell, 512)
        return 0

    def _synthesize(self, real_name: str) -> dict:
        iface, protocol = self._iface_for(real_name)
        return {
            "message_name": real_name,
            "interface": iface,
            "protocol": protocol,
            "timestamp": "<<timestamp>>",
            "m_tmsi": "<<m_tmsi>>",
            "cell_id": "<<cell_id>>",
            "procedure_id": "<<procedure_id>>",
            "decoding_status": "synthetic",
            "decoded": {"message": real_name},
            **self.ENVELOPE_DEFAULTS,
        }

    def _iface_for(self, real_name: str):
        if real_name.startswith("S1"):
            return "S1", "S1AP"
        if real_name.startswith("RRC") or real_name[:1].islower():
            return "RRC", "RRC"
        return "TWIN", "TWIN"

    def build(self, logical: str, *, ue_id: str, cell: str, txn=None, step=None, **twin) -> dict:
        """Build a realistic message for ``logical`` and attach the twin sidecar.

        ``twin`` carries simulation-only fields (demand_mbps, position, tx_power_dbm,
        rf, allocated_prbs, mcs, cause, congested, ...) consumed by the DU/UE; they
        are not part of real signalling.
        """
        real_name = self.real_name(logical)
        template = self.templates.get(real_name) or self._synthesize(real_name)
        ctx = self._token_context(ue_id, cell, real_name)
        msg = fill(template, ctx)
        msg["message_name"] = real_name           # ensure present even for odd templates
        msg["record_id"] = ctx["record_id"]       # canonical wire identifier
        if txn is not None:
            msg["txn"] = txn
        sidecar = {"logical": logical, "ue_id": ue_id, "cell": cell}
        if step is not None:
            sidecar["step"] = step
        sidecar.update({k: v for k, v in twin.items() if v is not None})
        msg["_twin"] = sidecar
        return msg


def twin(msg: dict) -> dict:
    """Convenience accessor for the simulation sidecar of a received message."""
    return msg.get("_twin") or {}
