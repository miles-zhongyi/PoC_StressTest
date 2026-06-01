#!/usr/bin/env bash
# Run the digital twin locally (no Docker).
#   ./scripts/run_local.sh [num_ues]      # default 1
# Stop with Ctrl-C (all children are cleaned up).
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"
ulimit -n 65536 2>/dev/null || true

N="${1:-1}"
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "starting DU ..."
python3 du/du_server.py & pids+=($!)
sleep 1
echo "starting RU ..."
RU_HOST=127.0.0.1 python3 ru/ru_server.py & pids+=($!)
sleep 1
echo "starting UE simulator with $N UE(s) ..."
RU_HOST=127.0.0.1 NUM_UES="$N" python3 ue/ue_sim.py & pids+=($!)

echo "running. status: curl http://127.0.0.1:8080/status"
wait
