#!/usr/bin/env bash
# Developer-only launch: enables LIVE remediation for THIS process only.
# The repository default stays KYBERNAUT_REMEDIATION_MODE=dry_run.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${KYBERNAUT_DEV_PORT:-8010}"   # must match the Vite proxy target (UI/vite.config.ts)

ctx="$(kubectl config current-context 2>/dev/null || true)"
if [ "$ctx" != "kind-kybernaut" ]; then
  echo "Refusing to start live mode: kubectl context is '${ctx}', expected 'kind-kybernaut'." >&2
  echo "Run: kubectl config use-context kind-kybernaut" >&2
  exit 1
fi

# The usual reason approvals stay dry_run: an OLD API process (started without
# the variable) is still bound to this port and keeps answering requests.
if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
  echo "Port ${PORT} is already in use by another process (probably an older dry_run API)." >&2
  echo "Stop that process first (do not leave two APIs running), then re-run this script." >&2
  exit 1
fi

export KYBERNAUT_REMEDIATION_MODE=live
# The lab auto-generator injects a scenario every 3 minutes and CLEANS UP the
# previous one, so a failure disappears before you can approve and execute and
# the incident closes as "condition cleared before remediation". Keep it off
# while demonstrating remediation (override: KYBERNAUT_LAB_AUTOGEN=true ./scripts/dev_live.sh).
export KYBERNAUT_LAB_AUTOGEN="${KYBERNAUT_LAB_AUTOGEN:-false}"
echo "Lab auto-generator: ${KYBERNAUT_LAB_AUTOGEN}"
echo "Starting API on 127.0.0.1:${PORT} with KYBERNAUT_REMEDIATION_MODE=live (context ${ctx})"
exec "${PYTHON:-.venv/bin/python}" -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --reload