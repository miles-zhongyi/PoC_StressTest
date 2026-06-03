# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A software digital twin of a small 5G cluster for capacity/load stress testing: a **DU** (scheduler, owns the PRB pool, does admission control), two **RUs** (radio units / cells), and a **UE simulator** that runs thousands of UEs as asyncio tasks in one process. There is no real radio or ASN.1 — message *names* mirror RRC/F1AP procedures but payloads are a simplified JSON wire protocol.

## Commands

This is a Python 3.12+ project with **no build step, no linter config, and no test suite**. It runs via Docker Compose. The host shell is Windows PowerShell.

```powershell
# Synthetic load (default), rebuild + run detached
docker compose up -d --build

# More UEs (PowerShell sets env before compose reads ${NUM_UES})
$env:NUM_UES = "500"; docker compose up -d --build
.\scripts\run_stress.ps1 -NumUes 500 -Detach   # helper wrapper

docker compose ps                 # expect du, ru, ru2, ue-sim, dashboard = Up
docker compose logs -f du         # watch PRB utilisation bars
docker compose logs -f ue-sim     # watch UE attach/handover stats
docker compose down

# Trace replay overlay: replay call traces once at startup, then synthetic
docker compose -f docker-compose.yml -f docker-compose.trace.yml up -d --build
```

Endpoints (note Windows-specific host ports):
- Dashboard: http://localhost:9090
- DU status JSON: http://localhost:9080/status — host **9080** maps to container 8080 (8080 is in Windows' excluded port range)

Run without Docker (single RU, Linux/macOS/Git Bash only): `./scripts/run_local.sh [num_ues]`.

### Building the trace index (host, Python required)

Replay needs a prebuilt index. Decoded trace JSON goes under `22_decoded/` (gitignored, large). Run from repo root with PYTHONPATH set or `common.*` imports fail:

```powershell
$env:PYTHONPATH = (Get-Location).Path
python scripts/build_trace_index.py --max-files 1 --out data/trace_index_sample.jsonl  # quick test
python scripts/build_trace_index.py --trace-dir 22_decoded --out data/trace_index.jsonl # full
```

Realistic-signalling **message templates** are built similarly (optional — the catalog ships built-in defaults):

```powershell
python scripts/build_message_templates.py --max-files 8 --out data/lte_templates.json
```

Run the test suite with `python -m pytest tests/` (needs `PYTHONPATH` set, or rely on `tests/conftest.py`).

## Architecture

### Signalling chain and connection model

```
UE  <--Uu-->  RU  <--F1-->  DU
```

The key design decision that makes scale possible: **connection multiplexing differs per hop.**

- **UE ↔ RU (Uu):** one TCP socket *per UE*. A UE's socket *is* its radio link — this is what makes handover a simple matter of redirecting a connection to a different RU, no cross-process state migration.
- **RU ↔ DU (F1):** exactly **one** multiplexed TCP connection per RU, carrying every UE's signalling. Messages are correlated by a per-message `txn` id. This mirrors real F1 (one DU↔RU association for all UEs) and keeps the DU at ~1 connection per RU instead of one per UE. See `F1Link` in [ru/ru_server.py](ru/ru_server.py) — a single reader task matches DU replies back to waiting requests via futures keyed on `txn`.

Every UE→DU message gets exactly one DU→UE reply, so the RU is a synchronous transparent proxy.

### Signalling layer ([common/signaling/](common/signaling/))

The wire messages are **realistic LTE signalling** (RRC + S1AP names/structures), not invented stand-ins. This is a pluggable layer above the transport:

- `procedures.py` — technology-neutral flow as ordered `Step(uplink, downlink, action)` tuples. Because the link is strict one-uplink→one-reply, a fuller call flow is modelled as a *sequence* of exchanges the UE drives: `ATTACH_FLOW` (RRC connection request → setup → setup complete → security → capability → **Initial Context Setup**), the steady-state `MEASUREMENT_STEP`/`DATA_STEP`, and `RELEASE_FLOW`. `action ∈ {none, admit, reconfig, release}` tells the DU what capacity work to do — admission runs at the context-setup step.
- `catalog.py` / `lte.py` — `LteCatalog` maps each logical name to a real message name + template, and `build()`/`classify()` convert to/from wire messages. `nr.py` is a reserved 5G stub that raises `NotImplementedError`. `get_catalog()` selects by `RADIO_TECH` (default `lte`) and caches.
- `templates.py` — a template is a real record's envelope+`decoded` body with per-instance leaves replaced by `<<token>>`; `fill()` substitutes live values. Real templates come from `data/lte_templates.json` (built offline) and **override** the catalog's built-in `DEFAULT_TEMPLATES`; a missing file just means defaults are used, so the stack always runs.

**Wire message shape**: realistic envelope (`message_name`, `interface`, `decoded`, …) + top-level `txn` (RU correlation) + a **`_twin` sidecar** holding simulation-only state real traces lack (`ue_id`, `cell` = DU pool key, `rf`, `demand_mbps`, `allocated_prbs`, `mcs`, `cause`, `step`). Everything functional lives in `_twin` so it never collides with the cosmetic realistic envelope (e.g. numeric `cell_id`). The DU classifies an uplink by its real `message_name`.

### The three servers (all asyncio)

- **[du/du_server.py](du/du_server.py)** — owns one `Cell` (PRB pool) per cell. `dispatch()` classifies each uplink to a flow `Step` via the catalog and routes by `action`; handlers (`handle_setup`, `handle_measurement`, `handle_data`, `handle_release`, `handle_passthrough`) read functional fields from the message's `_twin` sidecar and return realistic downlinks built via the catalog. They are **synchronous and never `await`**, so they are atomic under the single-threaded loop and mutate the PRB pool without locks. The HTTP status endpoint runs in a background thread serving a snapshot the event loop refreshes once a second. When an F1 link drops, the DU reclaims PRBs for every UE that RU was serving.
- **[ru/ru_server.py](ru/ru_server.py)** — turns UE geometry + tx power into RSRP/SINR (`compute_rf`) and stamps the serving cell + RF into the uplink's `_twin` sidecar before proxying to the DU. If a UE drops its Uu link without releasing, the RU sends a catalog-built S1 UE Context Release Request so PRBs are never leaked under churn.
- **[ue/ue_sim.py](ue/ue_sim.py)** — runs `NUM_UES` UEs as asyncio tasks. Each UE has its own identity, 2-D random-walk mobility (`Walk`), tx power, and traffic demand. `reconcile()` spawns/cancels tasks in batches (`SPAWN_BATCH`) to match `target_num_ues` without connection storms. Exposes `GET /status` and `POST /control {"num_ues": N}` for runtime scaling (used by the dashboard); the target is persisted to `UE_TARGET_STATE` so it survives container restart.

### Handover

The UE decides handover (A3-style hysteresis) in `one_session()`. It estimates RSRP from every RU using the **same path-loss model the RU uses** (`rsrp_from`, mirroring `rf_model`), so its decision matches RU reality. It only hands over when a neighbour beats the serving RU by `HO_MARGIN_DB` (default 3 dB), preventing ping-pong. Handover is **make-before-break**: the UE walks the full attach flow (`_attach`) on the target RU first, and only releases the source after the target admits it. The DU's `handle_setup` (run at the context-setup step) calls `_release_ue_other_cells` to avoid double-counting a UE on two cells during the overlap.

The two RUs are placed left/right of the origin (`RU_X` = −600 / +600 in compose) so UEs roaming across the midline trigger handovers.

### RF + capacity model ([common/rf_model.py](common/rf_model.py))

The chain: `distance + tx power → (path loss) → RSRP/SINR → (Shannon) → spectral efficiency → PRB requirement`. Single-layer (no MIMO), tuned for an n78 (3.5 GHz) 100 MHz macro cell. Beyond ~1.3 km a UE falls out of coverage (`MIN_SINR_DB`) and is rejected.

**Traffic profiles** drive admission sizing via `prbs_for_traffic`:
- `voip` (default) — voice; admission reserves a **fixed 1–2 PRBs per session** (1 PRB good RF, 2 PRBs marginal), *not* derived from `demand_mbps`. Capped at `VOIP_MAX_PRBS`.
- `data` — broadband; PRBs sized from `demand_mbps` (Mbps), no cap, for capacity stress.

To switch to broadband stress, set `TRAFFIC_PROFILE=data` plus `DEMAND_MIN_MBPS`/`DEMAND_MAX_MBPS` on **both** `du` and `ue-sim` (the DU sizes the grant, the UE sets the demand).

### Trace replay path

Real decoded call traces (huge, 100k+ records/file) are streamed and mapped to twin events by [common/call_trace.py](common/call_trace.py) (`iter_trace_events` → attach/measurement/release events keyed by `m_tmsi`/`procedure_id`). `scripts/build_trace_index.py` writes a compact JSONL index. At startup with `REPLAY_MODE=1`, the UE sim ([common/trace_replay.py](common/trace_replay.py)) picks UEs with full attach→release arcs and replays at trace-relative times (scaled by `REPLAY_SPEED`), then continues in synthetic mode. Replay uses the **same realistic LTE signalling** as synthetic mode (via the catalog) — the trace supplies only the timing, never raw ASN.1.

### Dashboard ([dashboard/server.py](dashboard/server.py))

Polls DU and UE-sim `/status` once a second, caches the last-good snapshot (serves stale data when a backend blips), serves the static UI, and proxies `POST /api/ues` → UE sim `/control` for the live UE-count slider.

It also renders a **live single-UE call-flow ladder**: the DU captures the real messages for one UE (locks onto a UE at its `RRC_CONNECTION_REQUEST`, follows it to release, then freezes; resettable) into a ring buffer exposed at `GET /trace` (and `GET /trace/reset`). The dashboard proxies these as `/api/trace[/reset]` and draws a sequence diagram (UE · RU · DU lifelines — RRC/TWIN arrows on the Uu side, S1AP on the core side); clicking an arrow shows that exact message's full JSON. The DU also exposes a static `/api/callflow` (catalog `describe()`) as a canonical-flow reference. Because the dashboard now imports `common.signaling`, its Dockerfile copies `common/`.

## Conventions and gotchas

- **The transport** ([common/protocol.py](common/protocol.py)) is 4-byte big-endian length prefix + UTF-8 JSON. Sync (`send_msg`/`recv_msg`) and asyncio (`async_send_msg`/`async_recv_msg`) variants exist. The *message content* is now produced by the signalling catalog (see above) — build messages with `catalog.build(...)` and classify with `catalog.classify(...)`; don't hardcode message names or hand-assemble dicts. The legacy `RRC_*`/`DATA` constants in `protocol.py` remain only as fallbacks.
- Each server inserts the repo root into `sys.path` at import time by walking up to find `common/`, so they run both as `python du/du_server.py` and inside their containers.
- Configuration is entirely environment variables, defaulted in code and overridden in [docker-compose.yml](docker-compose.yml). `REPLAY_MODE` is pinned to `0` in the base compose so a stray `$env:REPLAY_MODE` in the host shell can't hijack a synthetic run; the trace overlay flips it to `1`.
- One socket per UE means the fd limit matters: RUs and ue-sim set `ulimits.nofile` to 65536 in compose.
- `22_decoded/` (raw traces) and `data/trace_index.jsonl` are gitignored due to size; `data/trace_index_sample.jsonl` is committed as an example.
