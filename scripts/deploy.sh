#!/usr/bin/env bash
# Deploy job-digest from this Mac to the server (ops-10). Run by the product, never by cron.
#
#   JOBDIGEST_HOST=root@<server> scripts/deploy.sh            # deploy
#   JOBDIGEST_HOST=root@<server> scripts/deploy.sh --dry-run  # show what would change; touches nothing
#
# Order: 0) read-only server preflight: refuse if the morning job is running (exit 3) or the server .env
# lacks the Telegram keys (exit 4); 1) copy the code to a staging folder on the server and run py_compile +
# the whole test suite there (on the server's Python, with a scratch DB); 2) back up code, state.db, systemd
# unit and crontab; 3) copy staging -> /opt/job-digest (--delete for code paths only); 4) chmod +x run.sh,
# migrate the DB (store.connect), restart the bot; 5) checks (bot active, crontab lines, run.sh +x, unit
# matches deploy/, .env checksum unchanged); any failed check exits 5 and prints the rollback command.
# Any failure after the backup prints "PRODUCTION MAY BE HALF-DEPLOYED" with the rollback command.
# Exit codes: 1 generic failure, 2 bad usage or no host, 3 morning job running, 4 server .env missing keys,
# 5 post-deploy check failed.
# data/, out/ and .env are never copied in either direction: they are excluded on every rsync, and the
# server's .env checksum is compared before and after (values are never printed).

set -euo pipefail

HOST="${JOBDIGEST_HOST:-}"
APP="/opt/job-digest"
STAGE="/tmp/job-digest-stage"
STAMP="$(date +%Y-%m-%d-%H%M%S)"
BACKUP="/opt/job-digest-backups/$STAMP"
ROLLBACK="ssh $HOST \"tar xzf '$BACKUP/code.tgz' -C '$APP' && systemctl restart job-digest-bot\"  (state.db copy: $BACKUP/state.db)"
PHASE="before-backup"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"

# A failure after the backup may have swapped code in production; say so and give the rollback command.
# Before the backup nothing on the server has changed.
on_fail() {
  local rc="$1"
  if [ "$PHASE" = "after-backup" ]; then
    echo "PRODUCTION MAY BE HALF-DEPLOYED - rollback: $ROLLBACK" >&2
  else
    echo "deploy stopped before production was touched (exit $rc)" >&2
  fi
  exit "$rc"
}
trap 'on_fail $?' ERR
trap 'on_fail 130' INT
trap 'on_fail 143' TERM

# Argument check comes first: anything except no argument or exactly --dry-run exits before any ssh/rsync.
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--dry-run" ]; }; then
  echo "usage: JOBDIGEST_HOST=user@server scripts/deploy.sh [--dry-run]" >&2
  exit 2
fi
DRY=0
if [ "$#" -eq 1 ]; then DRY=1; fi

if [ -z "$HOST" ]; then
  echo "set JOBDIGEST_HOST=user@server" >&2
  exit 2
fi

EXCLUDES=(
  --exclude=/data/ --exclude=/out/ --exclude=.env --exclude='.env.*' --exclude=__pycache__/ --exclude='*.pyc'
  --exclude=/backups/ --exclude=/review/ --exclude=/specs/ --exclude=/docs/ --exclude=/research/
  --exclude=/.claude/ --exclude=/.impeccable/ --exclude=/cv/pilot/ --exclude='/scoring/today_*'
  --exclude=.DS_Store --exclude=.git/
)
# Protect rules: rsync --delete never removes these on the server, even if a future exclude is edited wrongly.
# Excluded paths are already protected from deletion; these make the guarantee explicit. Anchored to the root.
PROTECT=(--filter='P /data/' --filter='P /out/' --filter='P /.env' --filter='P /backups/')

say() { printf '\n== %s\n' "$*"; }

say "local checks"
bash -n "$LOCAL/run.sh"
[ -x "$LOCAL/run.sh" ] || { echo "run.sh is not executable locally" >&2; exit 1; }
python3 -m py_compile "$LOCAL"/*.py "$LOCAL"/scripts/*.py

# Read-only server preflight. The remote script comes in on stdin, so no quoting of its own is needed.
# Morning job: run.lock is held by run.sh (flock), or one of its Python steps is running.
# .env: only the presence of the two keys is checked, values are never printed.
IFS= read -r -d '' PRE_SH <<'REMOTE' || true
APP="$1"
cd "$APP" 2>/dev/null || { echo "JOB=unknown"; echo "ENV=missing"; exit 0; }
if ! flock -n "$APP/data/run.lock" -c true; then
  echo "JOB=running"
else
  rc=0; pgrep -f '[r]un\.sh|[c]ollect\.py|[s]core\.py|[e]nrich\.py|[d]igest\.py' >/dev/null || rc=$?
  case "$rc" in 0) echo "JOB=running";; 1) echo "JOB=idle";; *) echo "JOB=unknown";; esac
fi
missing=""
[ -s .env ] || missing=" .env-file"
for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  n="$(grep -c "^$k=.\+" .env 2>/dev/null || true)"
  [ "${n:-0}" -ge 1 ] || missing="$missing $k"
done
if [ -z "$missing" ]; then echo "ENV=ok"; else echo "ENV=missing:$missing"; fi
REMOTE

say "server preflight (read-only)"
PRE="$(printf '%s\n' "$PRE_SH" | ssh "$HOST" "bash -s -- '$APP'" || true)"
JOB="$(sed -n 's/^JOB=//p' <<<"$PRE")"
ENVS="$(sed -n 's/^ENV=//p' <<<"$PRE")"
case "$JOB" in
  idle) echo "morning job: not running" ;;
  running) echo "WARNING: morning job is running on the server (run.lock held or a job process found)" ;;
  *) echo "WARNING: could not verify the morning job state on the server" ;;
esac
if [ "$ENVS" = "ok" ]; then
  echo "server .env: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID present"
else
  echo "WARNING: server .env missing/empty - fix it first (${ENVS:-no answer from server})"
fi

if [ "$DRY" = 1 ]; then
  say "dry run: what would change in $APP (nothing is copied, nothing restarted)"
  rsync -azn -i --checksum --delete "${EXCLUDES[@]}" "${PROTECT[@]}" "$LOCAL/" "$HOST:$APP/"
  ssh "$HOST" "cat /etc/systemd/system/job-digest-bot.service" | diff - "$LOCAL/deploy/job-digest-bot.service" \
    && echo "systemd unit: unchanged" || echo "systemd unit differs (not installed by this script; install by hand)"
  exit 0
fi

if [ "$JOB" != "idle" ]; then
  echo "morning job is running on the server - refusing to deploy (exit 3). Deploy after the digest is sent." >&2
  exit 3
fi
if [ "$ENVS" != "ok" ]; then
  echo "server .env missing/empty - fix it first (exit 4)" >&2
  exit 4
fi

say "1/5 staging copy + tests on the server"
ssh "$HOST" "rm -rf '$STAGE' && mkdir -p '$STAGE'"
rsync -az --delete "${EXCLUDES[@]}" "$LOCAL/" "$HOST:$STAGE/"
ssh "$HOST" "set -e; cd '$STAGE'; test ! -e .env; test ! -e data/state.db;
  python3 -m py_compile *.py scripts/*.py;
  mkdir -p /tmp/job-digest-test-data /tmp/job-digest-test-out;
  rc=0; JOBDIGEST_DB=/tmp/job-digest-test-data/state.db JOBDIGEST_OUT=/tmp/job-digest-test-out \
    python3 -m unittest discover -s tests > /tmp/job-digest-tests.log 2>&1 || rc=\$?;
  tail -5 /tmp/job-digest-tests.log; if [ \$rc -ne 0 ]; then echo 'TESTS FAILED - production not touched'; exit 1; fi"

say "2/5 backup on the server -> $BACKUP"
ENV_SUM="$(ssh "$HOST" "test -s '$APP/.env' && sha256sum '$APP/.env' | cut -d' ' -f1")"
ssh "$HOST" "set -e; mkdir -p '$BACKUP';
  tar czf '$BACKUP/code.tgz' --exclude=./data --exclude=./out --exclude=./.env -C '$APP' .;
  cd '$APP' && python3 -c 'import sqlite3,sys; s=sqlite3.connect(\"file:data/state.db?mode=ro\", uri=True); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); print(\"state.db backed up\")' '$BACKUP/state.db';
  cp /etc/systemd/system/job-digest-bot.service '$BACKUP/' 2>/dev/null || true;
  crontab -l > '$BACKUP/crontab.txt' 2>/dev/null || true;
  ls -la '$BACKUP'"

# From here on production is changed by the deploy. The guard test is run on staging, so --delete never
# runs against an empty source. --delete only removes code paths: excluded paths and the P rules are kept.
PHASE="after-backup"
say "3/5 staging -> $APP (code paths only: files removed locally are removed on the server too)"
ssh "$HOST" "test -s '$STAGE/run.sh' && rsync -a --delete $(printf "'%s' " "${EXCLUDES[@]}" "${PROTECT[@]}") '$STAGE/' '$APP/'"

say "4/5 permissions, DB migration, bot restart"
ssh "$HOST" "set -e; cd '$APP'; chmod +x run.sh;
  python3 -c 'import store; store.connect().close(); print(\"migration ok\")';
  systemctl restart job-digest-bot; sleep 3"

say "5/5 checks"
IFS= read -r -d '' CHK_SH <<'REMOTE' || true
APP="$1"
cd "$APP" || exit 1
echo "run.sh mode: $(stat -c %a run.sh 2>/dev/null || echo missing)"
if [ -x run.sh ]; then echo "RUNSH=ok"; else echo "RUNSH=FAIL"; fi
BOT="$(systemctl is-active job-digest-bot 2>/dev/null || true)"
echo "bot: $BOT"
if [ "$BOT" = "active" ]; then echo "BOT=ok"; else echo "BOT=FAIL"; fi
echo "crontab:"
if crontab -l 2>/dev/null | grep job-digest; then echo "CRON=ok"; else echo "  NO job-digest lines in crontab"; echo "CRON=FAIL"; fi
test -s .env && echo '.env: present, non-empty' || echo '.env: MISSING OR EMPTY'
echo "ENVSUM=$(sha256sum .env 2>/dev/null | cut -d' ' -f1)"
sqlite3 data/state.db 'pragma integrity_check' 2>/dev/null || python3 -c 'import sqlite3; print(sqlite3.connect("data/state.db").execute("pragma integrity_check").fetchone()[0])'
REMOTE
CHK="$(printf '%s\n' "$CHK_SH" | ssh "$HOST" "bash -s -- '$APP'" || true)"
printf '%s\n' "$CHK"

FAILS=""
grep -qx 'RUNSH=ok' <<<"$CHK" || FAILS="$FAILS run.sh-not-executable"
grep -qx 'BOT=ok' <<<"$CHK" || FAILS="$FAILS bot-not-active"
grep -qx 'CRON=ok' <<<"$CHK" || FAILS="$FAILS crontab-lines-missing"
ENV_AFTER="$(sed -n 's/^ENVSUM=//p' <<<"$CHK")"
if [ -z "$ENV_SUM" ] || [ "$ENV_AFTER" != "$ENV_SUM" ]; then
  echo "WARNING: .env changed during deploy (or cannot be read) - restore from the server copy" >&2
  FAILS="$FAILS env-changed"
else
  echo ".env: unchanged by the deploy"
fi
if ssh "$HOST" "cat /etc/systemd/system/job-digest-bot.service" | diff -q - "$LOCAL/deploy/job-digest-bot.service" >/dev/null; then
  echo "systemd unit: unchanged (matches deploy/)"
else
  echo "systemd unit differs from deploy/ (not installed by this script; install by hand if intended)"
  FAILS="$FAILS unit-differs"
fi

if [ -n "$FAILS" ]; then
  echo "PRODUCTION MAY BE HALF-DEPLOYED - post-deploy checks failed ($FAILS )" >&2
  echo "rollback: $ROLLBACK" >&2
  exit 5
fi
echo "all post-deploy checks passed"
echo "rollback (if needed): $ROLLBACK"
