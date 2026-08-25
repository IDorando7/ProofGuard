#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${PROOFGUARD_REPO_ROOT:-/home/idorando/proofguard}"
DEMO_DIR="${REPO_ROOT}/demo"
STATE_DIR="${PROOFGUARD_GOLD_STATE_DIR:-${REPO_ROOT}/.gold_demo_state}"

BACKEND_HOST="${PROOFGUARD_GOLD_BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${PROOFGUARD_GOLD_BACKEND_PORT:-8000}"
FRONTEND_PORT="${PROOFGUARD_GOLD_FRONTEND_PORT:-5173}"

SCOPE_FILE="${PROOFGUARD_GOLD_SCOPE_FILE:-${DEMO_DIR}/proofguard_demo_target/scope.yaml}"
REPO_ZIP="${PROOFGUARD_GOLD_REPO_ZIP:-${DEMO_DIR}/proofguard_demo_target.zip}"
ARTIFACT_DIR="${PROOFGUARD_GOLD_ARTIFACT_DIR:-${DEMO_DIR}/demo_artifacts/gold_run}"

BACKEND_LOG="${DEMO_DIR}/demo_artifacts/backend.log"
FRONTEND_LOG="${DEMO_DIR}/demo_artifacts/frontend.log"

backend_pid=""
frontend_pid=""

cleanup() {
  echo
  echo "Stopping processes started by fresh_gold_demo.sh..."
  [[ -n "$frontend_pid" ]] && kill "$frontend_pid" 2>/dev/null || true
  [[ -n "$backend_pid" ]] && kill "$backend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

[[ -f "$SCOPE_FILE" ]] || { echo "Missing scope file: $SCOPE_FILE" >&2; exit 1; }
[[ -f "$REPO_ZIP" ]] || { echo "Missing repo zip: $REPO_ZIP" >&2; exit 1; }

if curl -fsS --max-time 1 "http://${BACKEND_HOST}:${BACKEND_PORT}/health" >/dev/null 2>&1; then
  echo "Backend already running. Stop it before a fresh run." >&2
  exit 1
fi

if curl -fsS --max-time 1 "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1; then
  echo "Frontend already running. Stop it before a fresh run." >&2
  exit 1
fi

"${DEMO_DIR}/reset_gold_demo.sh"
mkdir -p "${DEMO_DIR}/demo_artifacts"

echo "Starting backend..."
(
  cd "$REPO_ROOT/apps/audit-api"
  source "$REPO_ROOT/.venv/bin/activate"
  env \
    PROOFGUARD_DEMO_MODE=1 \
    AUDIT_API_DATA_DIR="${STATE_DIR}/audits" \
    AUDIT_API_PROTOCOL_DATA_DIR="${STATE_DIR}/protocol" \
    AUDIT_API_SQLITE_PATH="${STATE_DIR}/audit_api.sqlite3" \
    uvicorn app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
) >"$BACKEND_LOG" 2>&1 &
backend_pid=$!

for _ in $(seq 1 60); do
  curl -fsS "http://${BACKEND_HOST}:${BACKEND_PORT}/health" >/dev/null 2>&1 && break
  kill -0 "$backend_pid" 2>/dev/null || {
    echo "Backend exited. Log:" >&2
    tail -80 "$BACKEND_LOG" >&2 || true
    exit 1
  }
  sleep 0.5
done

curl -fsS "http://${BACKEND_HOST}:${BACKEND_PORT}/health" >/dev/null || {
  echo "Backend failed health check. See $BACKEND_LOG" >&2
  exit 1
}

echo "Starting frontend..."
(
  cd "${REPO_ROOT}/frontend"
  npm run dev -- --host 127.0.0.1 --port "$FRONTEND_PORT"
) >"$FRONTEND_LOG" 2>&1 &
frontend_pid=$!

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null 2>&1 && break
  kill -0 "$frontend_pid" 2>/dev/null || {
    echo "Frontend exited. Log:" >&2
    tail -80 "$FRONTEND_LOG" >&2 || true
    exit 1
  }
  sleep 0.5
done

curl -fsS "http://127.0.0.1:${FRONTEND_PORT}" >/dev/null || {
  echo "Frontend failed health check. See $FRONTEND_LOG" >&2
  exit 1
}

echo
echo "Frontend: http://localhost:${FRONTEND_PORT}"
echo "Starting interactive Gold Demo..."
echo

cd "$DEMO_DIR"
"${REPO_ROOT}/.venv/bin/python" proofguard_gold_demo.py \
  --base-url "http://${BACKEND_HOST}:${BACKEND_PORT}" \
  --frontend-url "http://localhost:${FRONTEND_PORT}" \
  --scope-file "$SCOPE_FILE" \
  --repo-zip "$REPO_ZIP" \
  --project-name "ProofGuard Management Gold Demo" \
  --budget 10000 \
  --artifact-dir "$ARTIFACT_DIR"
