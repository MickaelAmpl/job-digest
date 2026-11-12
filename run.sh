#!/usr/bin/env bash
# job-digest - cron entry point. Server layout: /opt/job-digest, stdlib-only Python, its own
# crontab lines. Cron has no environment, so everything is set explicitly here.
#
# Usage: run.sh collect   (06:30: emails + Adzuna + company feeds -> rules -> new only -> Claude scoring)
#        run.sh send      (07:30: Telegram digest, then the daily state.db backup)
#
# Nothing fails silently: a failed step is recorded by health.py and shown in the next digest's header;
# a skipped collect or a send that did not get through leaves an alert for the next digest.

set -euo pipefail

APP_DIR="/opt/job-digest"
LOG_DIR="$APP_DIR/data/logs"
LOCK_FILE="$APP_DIR/data/run.lock"
JOB_TIMEOUT=2400

export TZ="Europe/Vienna"
export HOME="/root"
export LANG="C.UTF-8"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

MODE="${1:-}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%F).log"
find "$LOG_DIR" -name '*.log' -mtime +30 -delete 2>/dev/null || true
cd "$APP_DIR"

log() { echo "---- $(date -Iseconds) :: $* ----" >>"$LOG_FILE" 2>/dev/null || true; }  # a full disk must not abort the morning
# Telegram line for a failed run (ops2-01): scripts/alert.py reads no DB, so it works when digest.py cannot.
tg_alert() { python3 scripts/alert.py "$1" >>"$LOG_FILE" 2>&1 || true; }
# One Telegram line per run for a collect step that stops or fails; the first one wins (ALERTED guards it).
ALERTED=0
stop_alert() {
  [ "$ALERTED" = 0 ] || return 0
  ALERTED=1
  tg_alert "Утренний сбор остановился на шаге $1. Подборка в 07:30 может быть неполной."
}
# Any other failure under set -e (a line outside the wrapped steps) is caught here too.
on_err() { [ "$MODE" = collect ] && stop_alert "run.sh, строка $1"; return 0; }
trap 'on_err $LINENO' ERR

# step [--timeout N] cmd...: run with a time limit; a non-zero exit is logged and recorded for the digest.
step() {
  local limit="$JOB_TIMEOUT"
  if [ "${1:-}" = "--timeout" ]; then limit="$2"; shift 2; fi
  log "$*"
  local rc=0
  timeout "$limit" "$@" >>"$LOG_FILE" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "FAILED (exit $rc): $*"
    python3 health.py step "$MODE" "$rc" "$*" >>"$LOG_FILE" 2>&1 || true
  fi
  return "$rc"
}

if [ ! -s "$APP_DIR/.env" ]; then
  log "ERROR: $APP_DIR/.env is missing or empty - nothing can be read or sent ($MODE)"
  python3 health.py alert "файл .env на сервере пуст или пропал ($MODE не выполнен)" >>"$LOG_FILE" 2>&1 || true
  exit 1
fi

exec 9>"$LOCK_FILE"
case "$MODE" in
  collect)
    # collect never waits: a still-running previous collect means this morning is skipped - and said so.
    if ! flock -w 0 9; then
      log "lock still held, skipping (collect) - the next digest says so"
      python3 health.py alert "утренний сбор $(date +%d.%m) пропущен: предыдущий ещё работал" >>"$LOG_FILE" 2>&1 || true
      tg_alert "Утренний сбор вакансий пропущен: предыдущий запуск ещё работает. Проверю."
      exit 0
    fi
    python3 health.py start >>"$LOG_FILE" 2>&1 || true
    # Claude login check (stage 11): an expired login is said in Telegram at once, not found in the morning cards.
    step --timeout 120 python3 scripts/claude_check.py || true
    # Each source step may fail on its own (a site down); later steps still run on what we have.
    # collect.py reads every day since the last successful read (min 2, max 15), so a missed morning is caught up.
    step --timeout 900 python3 collect.py 2 || true
    step --timeout 1200 python3 fetch_companies.py || true
    # prefilter and prepare stop the run when they fail (later steps need their output): one Telegram line, then stop.
    step python3 prefilter.py || { rc=$?; stop_alert prefilter.py; exit "$rc"; }
    step python3 prepare.py || { rc=$?; stop_alert prepare.py; exit "$rc"; }
    step python3 enrich.py || true    # has its own 25-min budget and saves after every job
    step python3 score.py || stop_alert score.py   # writes scores into data/state.db; digest reads from there; the run goes on to health finish
    # Sundays (stage 9): re-check the vacancies marked Интересно; a failure here never stops the morning.
    if [ "$(date +%u)" = 7 ]; then
      step --timeout 660 python3 liveness.py || true
    fi
    python3 health.py finish >>"$LOG_FILE" 2>&1 || true
    ;;
  send)
    # send waits up to 50 min for a still-running collect (enrich can be slow). If it is still running then,
    # the digest goes out anyway with what is already scored (digest.py has its own lock and only reads the DB);
    # the rest comes tomorrow, and the header says the morning run did not finish.
    if ! flock -w 3000 9; then
      log "lock still held after 3000s - sending what is already scored"
    fi
    digest_rc=0
    step python3 digest.py || digest_rc=$?
    if [ "$digest_rc" -eq 3 ]; then
      # digest.py delivered part of the digest and queued its own alert; say so now, not only tomorrow.
      tg_alert "Подборка сегодня дошла не полностью: часть вакансий не ушла в Telegram. Проверю и дошлю."
    elif [ "$digest_rc" -ne 0 ]; then
      tg_alert "Подборка сегодня не отправилась (ошибка на шаге отправки). Проверю и дошлю."
    fi
    # The backup is its own step (pipeline2-04, ops2-07): a failed copy is a backup alert, never a 'digest did not go out' one.
    backup_rc=0
    log "scripts/backup_db.py"
    timeout 600 python3 scripts/backup_db.py >>"$LOG_FILE" 2>&1 || backup_rc=$?
    if [ "$backup_rc" -ne 0 ]; then
      log "FAILED (exit $backup_rc): scripts/backup_db.py"
      python3 health.py backup-failed "$backup_rc" >>"$LOG_FILE" 2>&1 || true
    fi
    ;;
  *)
    echo "usage: run.sh collect|send" >&2
    exit 2
    ;;
esac
