#!/usr/bin/env sh
set -eu

ACTION="${1:-status}"
CONFIG_PATH="${CONFIG_PATH:-config/feishu_automation.local.json}"
PYTHON_BIN="${PYTHON_BIN:-python}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
LOG_DIR="$ROOT_DIR/logs"
STATE_DIR="$ROOT_DIR/state"
PID_FILE="$STATE_DIR/feishu-worker.pid"
WORKER_LOG="${WORKER_LOG:-$LOG_DIR/feishu-worker.log}"
MAX_LOG_BYTES="${MAX_LOG_BYTES:-5242880}"

mkdir -p "$LOG_DIR" "$STATE_DIR"

timestamp() {
  date "+%Y-%m-%d %H:%M:%S"
}

log_control() {
  printf '[%s] %s\n' "$(timestamp)" "$*" | tee -a "$WORKER_LOG"
}

rotate_log_if_needed() {
  log_file="$1"
  if [ ! -f "$log_file" ]; then
    return 0
  fi

  size="$(wc -c < "$log_file" 2>/dev/null || printf '0')"
  if [ "$size" -lt "$MAX_LOG_BYTES" ]; then
    return 0
  fi

  mv "$log_file" "$log_file.1"
}

is_running() {
  pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

read_pid() {
  if [ -f "$PID_FILE" ]; then
    sed -n '1p' "$PID_FILE"
  fi
}

start_worker() {
  existing_pid="$(read_pid || true)"
  if is_running "$existing_pid"; then
    log_control "Feishu worker is already running. pid=$existing_pid"
    exit 0
  fi

  if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
  fi

  rotate_log_if_needed "$WORKER_LOG"
  log_control "Starting Feishu worker. log=$WORKER_LOG"

  cd "$ROOT_DIR"
  nohup "$PYTHON_BIN" -u "scripts/feishu_task_runner.py" \
    --config "$CONFIG_PATH" \
    --mode watch \
    >> "$WORKER_LOG" 2>&1 &

  pid="$!"
  printf '%s\n' "$pid" > "$PID_FILE"
  log_control "Feishu worker started. pid=$pid"
}

stop_worker() {
  pid="$(read_pid || true)"
  if ! is_running "$pid"; then
    log_control "Feishu worker is not running."
    rm -f "$PID_FILE"
    return 0
  fi

  log_control "Stopping Feishu worker. pid=$pid"
  kill "$pid" 2>/dev/null || true

  count=0
  while is_running "$pid" && [ "$count" -lt 20 ]; do
    count=$((count + 1))
    sleep 1
  done

  if is_running "$pid"; then
    log_control "Worker did not stop in time; forcing stop. pid=$pid"
    kill -9 "$pid" 2>/dev/null || true
  fi

  rm -f "$PID_FILE"
  log_control "Feishu worker stopped."
}

status_worker() {
  pid="$(read_pid || true)"
  if is_running "$pid"; then
    log_control "Feishu worker is running. pid=$pid"
  else
    log_control "Feishu worker is not running."
    rm -f "$PID_FILE"
  fi
}

case "$ACTION" in
  start)
    start_worker
    ;;
  stop)
    stop_worker
    ;;
  restart)
    stop_worker
    start_worker
    ;;
  status)
    status_worker
    ;;
  *)
    printf 'Usage: %s {start|stop|restart|status}\n' "$0" >&2
    exit 2
    ;;
esac
