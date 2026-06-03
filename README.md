# 5G RU Digital Twin — Stress Test PoC

Software digital twin of a small 5G cluster: **DU** (scheduler / PRB admission), two **RUs** (cells), and a **UE simulator** (many UEs as async tasks in one process). Optional **dashboard** for live metrics and UE count control.

Traffic defaults to **VoIP-style** load: **1–2 PRBs per voice session** on the DU.

---

## Prerequisites

1. **Git** — clone this repository  
2. **Docker Desktop** — installed and **running** (wait until the tray icon says Docker is ready)  
3. **Python 3.12+** — only needed on the host for **Step A** (building the call-trace index)

Verify Docker:

```powershell
docker version
```

You should see both **Client** and **Server** sections.

---

## Project layout

| Path | Purpose |
|------|---------|
| `du/` | Distributed Unit — PRB pools per cell, admission control |
| `ru/` | Radio Unit server (used for `ru` and `ru2` containers) |
| `ue/` | UE simulator (synthetic or call-trace replay) |
| `common/` | Protocol, RF model, call-trace parsing |
| `dashboard/` | Web UI on port **9090** |
| `22_decoded/` | Raw decoded call-trace JSON (**local only**, gitignored) |
| `data/trace_index.jsonl` | Compact index for replay (**gitignored**, built in Step A) |
| `scripts/build_trace_index.py` | Build trace index from `22_decoded/` |
| `scripts/run_stress.ps1` | Helper to start compose with many UEs |

---

## Run the stack (synthetic mode — default)

Synthetic UEs: random mobility, attach / measurement / release on timers, handover between two RUs. **No call-trace files required.**

```powershell
cd path\to\poc_StressTest
docker compose up -d --build
```

Check services:

```powershell
docker compose ps
```

Expected: `du`, `ru`, `ru2`, `ue-sim`, `dashboard` — all **Up**.

| URL | What |
|-----|------|
| http://localhost:9090 | Dashboard (PRB bars, UE slider, handover stats) |
| http://localhost:9080/status | DU JSON status (use **9080**, not 8080 on Windows) |

**More UEs (PowerShell):**

```powershell
$env:NUM_UES = "50"
docker compose up -d --build
```

Or:

```powershell
.\scripts\run_stress.ps1 -NumUes 500 -Detach
```

**Logs:**

```powershell
docker compose logs -f du
docker compose logs -f ue-sim
```

**Stop:**

```powershell
docker compose down
```

---

## Realistic signalling (LTE call flow)

By default the UE↔RU↔DU exchange uses **real LTE signalling message names and
structures** (RRC + S1AP), not invented stand-ins. Each session walks a fuller call
flow — RRC connection request → setup → setup complete → security mode → UE
capability → **Initial Context Setup** (the PRB grant / admission point) → measurement
reconfiguration → S1 UE Context Release. The radio technology is selected by
`RADIO_TECH` (`lte` today; `nr` is reserved for 5G and raises a clear error until
implemented). See `common/signaling/` and `CallFlow/` for the reference flows.

Messages are built from **templates**: the per-message-type envelope + `decoded`
body observed in the real traces, with per-instance fields (UE m-TMSI, cell id,
timestamp, S1AP ids …) filled in. Simulation-only state the traces lack (demand, RF,
allocated PRBs) rides in a `_twin` sidecar.

**Templates work out of the box** from built-in defaults. To use templates extracted
from the real traces under `22_decoded/`, build the index once (host):

```powershell
$env:PYTHONPATH = (Get-Location).Path
python scripts/build_message_templates.py --max-files 8 --out data/lte_templates.json
```

The compose file mounts `./data` into `du`, `ru`, `ru2`, `ue-sim` and points
`LTE_TEMPLATES` at `data/lte_templates.json`; a missing file simply falls back to the
built-in defaults. Run the signalling/flow tests with `python -m pytest tests/`.

---

## Call-trace replay (real signalling timing)

Use this when you have decoded call traces under `22_decoded/` and want the UE simulator to drive attach / measurement / release at **times taken from the trace**, instead of synthetic timers.

Replay uses the same realistic LTE signalling messages as synthetic mode (see above); the trace only supplies the *timing* of each event, not raw ASN.1 from the files.

### Step A — Build the trace index (on the host, once)

Place decoded JSON under `22_decoded/` (folder is gitignored due to size).

**Quick test** (one file):

```powershell
cd path\to\poc_StressTest
$env:PYTHONPATH = (Get-Location).Path
python scripts/build_trace_index.py --max-files 1 --out data/trace_index_sample.jsonl
```

**Full index** (all `*_TRC_decoded.json` files — can take a long time):

```powershell
$env:PYTHONPATH = (Get-Location).Path
python scripts/build_trace_index.py --trace-dir 22_decoded --out data/trace_index.jsonl
```

Output: `data/trace_index.jsonl` — one JSON object per line (`attach` / `measurement` / `release` with timestamp and UE id).

### Step B — Run the twin with trace replay (Docker)

Ensure `data/trace_index.jsonl` exists (from Step A).

```powershell
cd path\to\poc_StressTest
$env:NUM_UES = "20"       # traced UEs to pick from index (if index has many UEs)
docker compose -f docker-compose.yml -f docker-compose.trace.yml up -d --build
```

Trace replay runs once at startup, then the stack continues in **synthetic** mode with `NUM_UES` tasks.

Watch replay:

```powershell
docker compose logs -f ue-sim
```

Open the dashboard: http://localhost:9090

**Return to synthetic-only mode** (dashboard stress test, no trace):

```powershell
Remove-Item Env:REPLAY_MODE -ErrorAction SilentlyContinue
docker compose up -d --build
```

`docker-compose.yml` pins `REPLAY_MODE=0` so a leftover `$env:REPLAY_MODE=1` in PowerShell cannot hijack the run.

---

## Configuration (environment)

Set on `ue-sim` / `du` in `docker-compose.yml` or via shell before `docker compose up`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `NUM_UES` | `1` | Target number of UEs (synthetic tasks or replay count) |
| `TRAFFIC_PROFILE` | `voip` | `voip` → 1–2 PRBs/session; `data` → Mbps stress (no PRB cap) |
| `REPLAY_MODE` | `0` | `1` = use `TRACE_INDEX` timing |
| `TRACE_INDEX` | `/trace/data/trace_index.jsonl` | Path inside container (host: `./data/trace_index.jsonl`) |
| `REPLAY_SPEED` | `10` | Trace replay speed multiplier |
| `DEMAND_MIN_MBPS` / `DEMAND_MAX_MBPS` | `0.012` / `0.048` | VoIP bitrate range (synthetic / replay) |

For broadband stress, set `TRAFFIC_PROFILE=data` and e.g. `DEMAND_MIN_MBPS=5`, `DEMAND_MAX_MBPS=30` on **both** `du` and `ue-sim`.

---

## Run without Docker (optional)

Linux / macOS / Git Bash — single RU only:

```bash
./scripts/run_local.sh 1
curl http://127.0.0.1:8080/status
```

---

## Troubleshooting

| Problem | What to do |
|---------|------------|
| `dockerDesktopLinuxEngine: The system cannot find the file specified` | Start **Docker Desktop** and wait until it is fully running |
| `compose up` hangs after “Recreated” | `Ctrl+C`, then `docker compose down` and `docker compose up -d --build` |
| Dashboard missing UE slider | Open **http://localhost:9090**, hard refresh **Ctrl+F5**, rebuild: `docker compose build dashboard` |
| Port 8080 fails on Windows | Use **9080** for DU status (already mapped in compose) |
| Trace replay: “index not found” | Run **Step A** so `data/trace_index.jsonl` exists; check volume `./data:/trace/data` in compose |
| `ModuleNotFoundError: common.*` when building index | Run from repo root with `$env:PYTHONPATH = (Get-Location).Path` |

---

## Clone + first-time checklist

1. `git clone <repo-url>` && `cd poc_StressTest`  
2. Start **Docker Desktop**  
3. `docker compose up -d --build`  
4. Open http://localhost:9090  
5. *(Optional)* Put traces in `22_decoded/` → **Step A** → **Step B** with `REPLAY_MODE=1`  
6. `docker compose down` when finished  

---

## License / data

Call-trace source files under `22_decoded/` are not stored in git. Share them separately or regenerate the index locally with `scripts/build_trace_index.py`.
