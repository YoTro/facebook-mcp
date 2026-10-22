#!/usr/bin/env bash
#
# Launch Chrome with the DevTools protocol enabled, isolated per account.
#
# Each account gets its own Chrome profile under
#   ~/.facebook-mcp/accounts/<account>/chrome
# so two Facebook accounts never share cookies, and the user's everyday Chrome
# is never touched (no need to quit it).
#
# Chrome >= 136 refuses --remote-debugging-port with the *default* profile, so a
# dedicated --user-data-dir is mandatory. On first run you can seed the profile
# with cookies from your main Chrome installation (--seed), which carries an
# existing Facebook login over without copying the whole 13 GB profile.
#
# Usage:
#   tools/launch_chrome_debug.sh                     # account "default", port 9222
#   tools/launch_chrome_debug.sh --account work      # port 9223 (auto)
#   tools/launch_chrome_debug.sh --account work --seed
#   tools/launch_chrome_debug.sh --account work --stop
#   tools/launch_chrome_debug.sh --account work --dry-run   # print the plan only
#
set -euo pipefail

ACCOUNT="${FB_ACCOUNT:-default}"
PORT=""
SEED=0
STOP=0
DRY_RUN=0
PROFILE_SOURCE=""

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MAIN_PROFILE="$HOME/Library/Application Support/Google/Chrome"
PYTHON="${PYTHON:-python3}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }

usage() {
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="${2:-}"; shift 2 ;;
    --port)    PORT="${2:-}"; shift 2 ;;
    --seed)    SEED=1; shift ;;
    --seed-from) PROFILE_SOURCE="${2:-}"; SEED=1; shift 2 ;;
    --stop)    STOP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -x "$CHROME" ]] || die "Chrome not found at: $CHROME"

# Must match facebook_mcp.core.accounts.ACCOUNT_NAME_RE.
[[ "$ACCOUNT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
  || die "invalid account name '$ACCOUNT' (letters, digits, '.', '_', '-'; max 64)"

CRED_ROOT="${FB_HOME:-$HOME/.facebook-mcp}"
ACCOUNT_DIR="$CRED_ROOT/accounts/$ACCOUNT"
PROFILE_DIR="$ACCOUNT_DIR/chrome"
META_FILE="$ACCOUNT_DIR/account.json"
LOG_FILE="$ACCOUNT_DIR/chrome.log"

mkdir -p "$ACCOUNT_DIR"
chmod 700 "$CRED_ROOT" "$CRED_ROOT/accounts" "$ACCOUNT_DIR" 2>/dev/null || true

# --- helpers ---------------------------------------------------------------

read_meta() {  # read_meta <key>
  [[ -f "$META_FILE" ]] || return 0
  "$PYTHON" - "$META_FILE" "$1" <<'PY'
import json, sys
path, key = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except Exception:
    raise SystemExit(0)
value = data.get(key)
if value is not None:
    print(value)
PY
}

write_meta() {  # write_meta <key> <value>  (atomic, keeps other keys)
  "$PYTHON" - "$META_FILE" "$1" "$2" <<'PY'
import json, os, sys, tempfile
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data[key] = int(value) if value.isdigit() else value
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

devtools_alive() {  # devtools_alive <port>
  curl -s -m 1 "http://127.0.0.1:$1/json/version" >/dev/null 2>&1
}

# PIDs of *this* account's Chrome.
#
# pgrep -f treats its pattern as an extended regex, so an unescaped path lets a
# legal account name like "a.b" match the "axb" account, and --stop would then
# kill the wrong browser. Escape the path and require an argument boundary so a
# path that is merely a prefix of another cannot match either.
account_chrome_pids() {
  local escaped
  escaped="$(printf '%s' "$PROFILE_DIR" | sed 's/[][\\.^$*+?(){}|]/\\&/g')"
  pgrep -f -- "--user-data-dir=${escaped}([[:space:]]|$)" 2>/dev/null || true
}

# --- --stop ----------------------------------------------------------------

if [[ "$STOP" -eq 1 ]]; then
  pids="$(account_chrome_pids)"
  if [[ -z "$pids" ]]; then
    note "account '$ACCOUNT' has no running Chrome"
    exit 0
  fi
  note "stopping Chrome for account '$ACCOUNT' ($(echo "$pids" | tr '\n' ' '))"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 1
  pids="$(account_chrome_pids)"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
  note "stopped"
  exit 0
fi

# --- resolve port ----------------------------------------------------------

# Already running? Report *our* instance on whatever port it took. A live
# DevTools listener alone says nothing about whose browser it is.
if [[ -n "$(account_chrome_pids)" ]]; then
  live="$(read_meta debug_port || true)"
  [[ -n "$PORT" ]] || PORT="$live"
  if [[ -n "$live" ]] && devtools_alive "$live"; then
    note "account '$ACCOUNT' already running on port $live"
    note "cdp_url : http://127.0.0.1:$live"
    exit 0
  fi
fi

# Pick a port that no other account has claimed and nothing is listening on.
# A positional "index" scheme collides as soon as an account is inserted, and
# trusting any listener on the computed port attaches to the wrong browser.
if [[ -z "$PORT" ]]; then
  PORT="$("$PYTHON" - "$CRED_ROOT" "$ACCOUNT" <<'PORTPY'
import json, os, socket, sys

root, account = sys.argv[1], sys.argv[2]
accounts_dir = os.path.join(root, "accounts")


def read_meta(name):
    try:
        with open(os.path.join(accounts_dir, name, "account.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def port_of(meta):
    port = meta.get("debug_port")
    if not port and isinstance(meta.get("cdp_url"), str):
        port = meta["cdp_url"].rsplit(":", 1)[-1]
    return int(port) if str(port).isdigit() else None


def free(candidate):
    if candidate in claimed:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", candidate)) != 0


claimed = set()
if os.path.isdir(accounts_dir):
    for name in os.listdir(accounts_dir):
        if name == account:
            continue  # our own recorded port is a candidate, not a conflict
        other = port_of(read_meta(name))
        if other is not None:
            claimed.add(other)

# Reuse our own recorded port when it is still available.
preferred = port_of(read_meta(account))
if preferred is not None and 1024 <= preferred < 65536 and free(preferred):
    print(preferred)
    raise SystemExit(0)

for candidate in range(9222, 9300):
    if free(candidate):
        print(candidate)
        raise SystemExit(0)

raise SystemExit("no free port in 9222-9299")
PORTPY
)"
  [[ -n "$PORT" ]] || die "could not allocate a free DevTools port"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "port $PORT is already in use by another process (try --port <other>)"
fi

# --- optional: seed cookies from the everyday Chrome ----------------------

SEED_DONE=0
if [[ "$SEED" -eq 1 ]]; then
  source_profile="${PROFILE_SOURCE:-$MAIN_PROFILE/Default}"
  cookies_src="$source_profile/Cookies"
  [[ -f "$cookies_src" ]] || cookies_src="$source_profile/Network/Cookies"
  [[ -f "$cookies_src" ]] || die "no Cookies database under $source_profile"

  if [[ -f "$PROFILE_DIR/Default/Network/Cookies" || -f "$PROFILE_DIR/Default/Cookies" ]]; then
    note "profile already exists; skipping --seed"
  else
    note "seeding profile from $source_profile"
    mkdir -p "$PROFILE_DIR/Default/Network"
    cp "$cookies_src" "$PROFILE_DIR/Default/Network/Cookies"
    cp "$cookies_src" "$PROFILE_DIR/Default/Cookies"
    if [[ -f "$MAIN_PROFILE/Local State" ]]; then
      cp "$MAIN_PROFILE/Local State" "$PROFILE_DIR/Local State"
    fi
    chmod -R go-rwx "$PROFILE_DIR"
    SEED_DONE=1
    note "  cookies copied (decrypted with the Keychain key, same user/machine)"
  fi
fi

# --- launch ---------------------------------------------------------------

if [[ "$DRY_RUN" -eq 1 ]]; then
  note "account : $ACCOUNT"
  note "profile : $PROFILE_DIR"
  note "port    : $PORT"
  note "cdp_url : http://127.0.0.1:$PORT"
  note "(dry run: nothing launched)"
  exit 0
fi

note "account : $ACCOUNT"
note "profile : $PROFILE_DIR"
note "port    : $PORT"
note "log     : $LOG_FILE"

"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$PROFILE_DIR" \
  --profile-directory=Default \
  --no-first-run \
  --no-default-browser-check \
  --disable-features=Translate \
  >"$LOG_FILE" 2>&1 &

for _ in $(seq 1 60); do
  if devtools_alive "$PORT"; then
    break
  fi
  sleep 0.5
done

devtools_alive "$PORT" || {
  note ""
  note "DevTools did not come up on port $PORT. Last log lines:"
  tail -20 "$LOG_FILE" 2>/dev/null || true
  die "launch failed"
}

write_meta cdp_url "http://127.0.0.1:$PORT"
write_meta debug_port "$PORT"

note ""
note "ready: $(curl -s "http://127.0.0.1:$PORT/json/version" \
  | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["Browser"])' 2>/dev/null || echo "Chrome")"

if [[ "$SEED_DONE" -eq 1 ]]; then
  note ""
  note "first run: the browser should already be logged in to Facebook."
  note "if it is not, log in once — the profile keeps the session for next time."
fi

cat <<EOF

next steps
  1. export this account's cookies for browser-free operation:
       .venv/bin/python tools/export_cookies.py --account $ACCOUNT
     or point the server at this browser (no export needed):
       FB_TRANSPORT_MODE=auto FB_ACCOUNT=$ACCOUNT .venv/bin/facebook-mcp
  2. stop this instance:
       tools/launch_chrome_debug.sh --account $ACCOUNT --stop
EOF
