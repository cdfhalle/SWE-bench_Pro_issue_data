#!/bin/bash
# One-shot setup for a fresh checkout. Idempotent and safe to re-run: it never
# overwrites an existing .env, it only rebuilds the enroot sysconf mirror.
#
#   bash bin/setup.sh
#
# Does the two things that cannot be committed because they are per-user:
#   1. builds enroot_sysconf/ (bakes THIS checkout's absolute path, so re-run
#      after moving the checkout)
#   2. writes ~/.config/mini-swe-agent/.env with your paths already filled in
#
# Leaves exactly one manual step: pasting a model API key.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve mini's global config file the same way minisweagent does, so an
# MSWEA_GLOBAL_CONFIG_DIR / XDG_CONFIG_HOME override is honoured here too.
CONFIG_DIR="${MSWEA_GLOBAL_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/mini-swe-agent}"
ENV_FILE="$CONFIG_DIR/.env"

echo "== building enroot sysconf mirror =="
bash "$REPO/slurm/setup_enroot_sysconf.sh" "$REPO/enroot_sysconf"

env_body() {
    cat <<EOF
# Written by $REPO/bin/setup.sh -- re-run it if you move the checkout.
ENROOT_DATA_PATH=/tmp/enroot-$USER/data
ENROOT_CACHE_PATH=/sc/scratch/$USER/swebp/enroot/cache
ENROOT_SYSCONF_PATH=$REPO/enroot_sysconf
OPENROUTER_API_KEY=sk-or-v1-REPLACE_ME
EOF
}

echo
if [ -e "$ENV_FILE" ]; then
    echo "== $ENV_FILE exists -- not touching it =="
    echo "Make sure it contains these values (ENROOT_SYSCONF_PATH especially):"
    echo
    env_body | grep -v OPENROUTER_API_KEY | sed 's/^/    /'
else
    mkdir -p "$CONFIG_DIR"
    umask 077
    env_body > "$ENV_FILE"
    echo "== wrote $ENV_FILE =="
    echo
    echo "Remaining manual step: replace sk-or-v1-REPLACE_ME with your OpenRouter key."
    echo "(Generating against a self-hosted server instead? Drop the line and pass"
    echo " --api-base, or let SWEBP_ENDPOINT_DIR discover a model-hosting endpoint.)"
fi

echo
echo "Then check your setup with:  uv run python -m swebp_slurm.submit_batch --help"
