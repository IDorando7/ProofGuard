#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${PROOFGUARD_REPO_ROOT:-/home/idorando/proofguard}"
DEMO_DIR="${REPO_ROOT}/demo"
STATE_DIR="${PROOFGUARD_GOLD_STATE_DIR:-${REPO_ROOT}/.gold_demo_state}"
ARTIFACTS_DIR="${DEMO_DIR}/demo_artifacts/gold_run"
ARCHIVE_DIR="${DEMO_DIR}/demo_artifacts/archive"
ARCHIVE=0

if [[ "${1:-}" == "--archive" ]]; then
  ARCHIVE=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--archive]"
  exit 2
fi

die() {
  echo "ERROR: $*" >&2
  exit 1
}

[[ -d "$REPO_ROOT" ]] || die "Repository does not exist: $REPO_ROOT"
[[ "$STATE_DIR" == "$REPO_ROOT"/* ]] || die "State dir must live under repo root: $STATE_DIR"

state_base="$(basename "$STATE_DIR")"
if [[ "$state_base" != *demo* && "$state_base" != *gold* ]]; then
  die "Refusing to delete suspicious state dir: $STATE_DIR"
fi

if curl -fsS --max-time 1 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "ERROR: backend is still running on port 8000. Stop it first." >&2
  exit 1
fi

echo "ProofGuard Gold Demo reset"
echo "  repo:      $REPO_ROOT"
echo "  state:     $STATE_DIR"
echo "  artifacts: $ARTIFACTS_DIR"
echo

if [[ "$ARCHIVE" -eq 1 && -d "$ARTIFACTS_DIR" ]] && find "$ARTIFACTS_DIR" -mindepth 1 -print -quit | grep -q .; then
  ts="$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$ARCHIVE_DIR"
  archive_path="${ARCHIVE_DIR}/gold_run_${ts}"
  echo "Archiving previous run -> $archive_path"
  cp -a "$ARTIFACTS_DIR" "$archive_path"
fi

echo "Removing dedicated Gold Demo state..."
rm -rf -- "$STATE_DIR"
mkdir -p "$STATE_DIR"

echo "Removing Gold Run artifacts..."
rm -rf -- "$ARTIFACTS_DIR"
mkdir -p "$ARTIFACTS_DIR"

if [[ "${PROOFGUARD_GOLD_CLEAR_VITE_CACHE:-0}" == "1" ]]; then
  rm -rf -- "${REPO_ROOT}/frontend/node_modules/.vite"
fi

cat <<EOF

RESET COMPLETE

Next start backend with THE SAME state directory:

  export PROOFGUARD_DEMO_MODE=1
  export AUDIT_API_DATA_DIR="$STATE_DIR/audits"
  export AUDIT_API_PROTOCOL_DATA_DIR="$STATE_DIR/protocol"
  export AUDIT_API_SQLITE_PATH="$STATE_DIR/audit_api.sqlite3"
  cd "$REPO_ROOT/apps/audit-api"
  source .venv/bin/activate
  uvicorn app.main:app --host 127.0.0.1 --port 8000

Gold Demo uses isolated audit and protocol storage under the directory above.
EOF
