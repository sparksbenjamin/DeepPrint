#!/bin/sh
# DeepPrint self-update.
#
# Refreshes the sources (personas, base Compose templates, engine, and this
# script itself), then re-applies the most recently deployed persona using
# the answers saved at deploy time. A normal run:
#
#   ./update.sh                        # inside a git clone of DeepPrint
#   sh ~/.deepprint/update.sh          # inside a bootstrap (zip) install
#   sh update.sh                       # fetched standalone; finds ~/.deepprint
#
# Useful variants:
#   ./update.sh --dry-run   # pull updates, then preview the re-render only
#   ./update.sh --force      # re-apply the deployment even if nothing was pulled
#   DEEPPRINT_PYTHON=python3.11 ./update.sh
#
# Git clones update with a fast-forward-only pull; local modifications abort
# the run. Bootstrap installs (curl ... | python3 -) have no git history, so
# they download the latest release zip and swap it in place, preserving the
# saved deployment state and any custom personas.
#
# Standalone runs (script fetched on its own, on any machine) fall back to the
# bootstrap install at $DEEPPRINT_HOME (default ~/.deepprint). Installs made
# by the legacy bootstrap — before it learned to self-update — get the current
# bootstrap fetched and run against the existing install, so they upgrade in
# place too instead of being wiped by a re-install.
#
# The saved deployment state lives next to the rendered Compose output (e.g.
# ~/tpotce/.deepprint_state.json) and is (re)written by every successful
# --deploy / --update run, so it survives repository refreshes. The git
# history is never rewritten: updates are fast-forward only, and local
# modifications abort the run instead of being merged.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENGINE="$SCRIPT_DIR/DeepPrint/deepprint.py"
PY=${DEEPPRINT_PYTHON:-python3}

say() { printf '[update.sh] %s\n' "$*"; }
die() { printf '[update.sh] ERROR: %s\n' "$*" >&2; exit 1; }

FORCE=0
ENGINE_ARGS=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        --dry-run) ENGINE_ARGS="$ENGINE_ARGS $arg" ;;
        *) die "unknown option: $arg (supported: --dry-run, --force)" ;;
    esac
done

# --- pick the refresh strategy: git clone, bootstrap install next to this
# --- script, or a bootstrap install at the default root (update.sh fetched
# --- standalone, e.g. `curl -fsSL .../update.sh | sh`) -----------------------
DEEPPRINT_ROOT="${DEEPPRINT_HOME:-$HOME/.deepprint}"
if command -v git >/dev/null 2>&1 \
    && git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    :
elif [ -f "$SCRIPT_DIR/bootstrap.py" ]; then
    [ ! -d "$SCRIPT_DIR/.git" ] \
        || die "git repository detected but git is not installed; install git or re-run bootstrap.py."
    say "bootstrap install detected; refreshing sources from the release zip."
    exec "$PY" "$SCRIPT_DIR/bootstrap.py" --update "$@"
elif [ -f "$DEEPPRINT_ROOT/bootstrap.py" ] && [ -d "$DEEPPRINT_ROOT/DeepPrint" ]; then
    if grep -q -- "--update" "$DEEPPRINT_ROOT/bootstrap.py" 2>/dev/null; then
        say "bootstrap install detected at $DEEPPRINT_ROOT; refreshing sources from the release zip."
        exec "$PY" "$DEEPPRINT_ROOT/bootstrap.py" --update "$@"
    fi
    # The installed bootstrap predates self-update; fetching the current one
    # and running it against the existing install preserves saved state and
    # custom personas (the legacy bootstrap wiped the install on every
    # re-run, and never installed update.sh).
    say "legacy install detected at $DEEPPRINT_ROOT; fetching the current updater."
    BOOTSTRAP_URL="${DEEPPRINT_BOOTSTRAP_URL:-https://raw.githubusercontent.com/sparksbenjamin/DeepPrint/main/bootstrap.py}"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$BOOTSTRAP_URL" | "$PY" - --update "$@"
        exit $?
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$BOOTSTRAP_URL" | "$PY" - --update "$@"
        exit $?
    fi
    die "install at $DEEPPRINT_ROOT predates self-update and neither curl nor wget is available; run bootstrap.py from a current checkout instead."
else
    die "no DeepPrint install found (looked in $SCRIPT_DIR and $DEEPPRINT_ROOT); re-install DeepPrint via bootstrap.py."
fi

BRANCH=$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD)
[ "$BRANCH" != "HEAD" ] || die "detached HEAD checkout; run: git -C '$SCRIPT_DIR' checkout main"

if ! git -C "$SCRIPT_DIR" diff --quiet || ! git -C "$SCRIPT_DIR" diff --cached --quiet; then
    die "local changes present in $SCRIPT_DIR; commit or stash them, then re-run."
fi

# --- 1. update the repository: personas, templates, engine, and this script --
say "checking origin for updates ($BRANCH)..."
BEFORE=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
git -C "$SCRIPT_DIR" fetch origin "$BRANCH"
git -C "$SCRIPT_DIR" merge --ff-only FETCH_HEAD
AFTER=$(git -C "$SCRIPT_DIR" rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    if [ "$FORCE" -eq 1 ]; then
        say "--force given; re-applying deployment from $AFTER."
    else
        say "already up to date ($AFTER); deployment left untouched."
        exit 0
    fi
else
    say "updated $BEFORE -> $AFTER."
fi

# --- 2. re-apply the saved deployment with the freshly updated engine -------
[ -f "$ENGINE" ] || die "engine not found at $ENGINE; repository layout changed, re-clone."

# The state file must predate this run: --update replays the answers recorded
# by the last --deploy. If it is missing, the engine explains what to do.
exec "$PY" "$ENGINE" --update$ENGINE_ARGS
