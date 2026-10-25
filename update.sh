#!/bin/sh
# DeepPrint self-update.
#
# Refreshes the repository (personas, base Compose templates, engine, and this
# script itself) from its git origin, then re-applies the most recently
# deployed persona using the answers saved at deploy time. A normal run:
#
#   ./update.sh
#
# Useful variants:
#   ./update.sh --dry-run   # pull updates, then preview the re-render only
#   ./update.sh --force      # re-apply the deployment even if nothing was pulled
#   DEEPPRINT_PYTHON=python3.11 ./update.sh
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

command -v git >/dev/null 2>&1 || die "git not found; re-clone DeepPrint instead of updating."

git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "$SCRIPT_DIR is not a git clone; re-clone and re-deploy with --deploy."

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
