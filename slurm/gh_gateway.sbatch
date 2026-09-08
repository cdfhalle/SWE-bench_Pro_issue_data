#!/bin/bash
# The issue-tracker gateway for one run. Start it BEFORE submitting the
# treatment arm; gen tasks fail fast (GH_GATEWAY_DOWN) rather than quietly
# running as baseline if it is not up.
#
#   sbatch --export=ALL,RUN=ghctx-on slurm/gh_gateway.sbatch
#   sbatch --export=ALL,RUN=ghctx-on,DIFFS=1 slurm/gh_gateway.sbatch   # + PR diffs
#   sbatch --export=ALL,RUN=ghctx-on,REPLAY=1 slurm/gh_gateway.sbatch  # cache only
#
# One process serves the whole array: it holds the token, applies the
# same-repo/pre-cutoff filters, and is the single accounting point for GitHub's
# rate limits (the Search API allows 30 requests/minute per token). It writes
# endpoint/gh-gateway-<RUN>.json on bind and removes it on exit, the same
# discovery contract the model-serving repo uses.
#
# CPU-only and near-idle: it is a filter in front of an HTTP API, not a worker.
#SBATCH --job-name=swebp-ghgw
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=24:00:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

RUN="${RUN:?set RUN=<run name> (the bare name, matching submit_batch --run)}"
RUN="${RUN##*/}"

# The PAT lives in ~/.config/mini-swe-agent/.env, which minisweagent loads on
# import -- the same route the ENROOT_* paths take in gen_array.sbatch. It is
# owner-only shared state; nothing here writes to it.
eval "$("$SWEBP_PYTHON" -c '
import minisweagent, os
if os.environ.get("GITHUB_TOKEN"):
    print("export GITHUB_TOKEN=" + os.environ["GITHUB_TOKEN"])
' | grep '^export ')"

if [ -z "${GITHUB_TOKEN:-}" ] && [ -z "${REPLAY:-}" ]; then
  echo "GITHUB_TOKEN is not set (add it to ~/.config/mini-swe-agent/.env)."
  echo "Unauthenticated is 60 requests/hour, which is not usable here."
  exit 1
fi

mkdir -p "$SWEBP_GH_ENDPOINT_DIR" "$SWEBP_GH_CACHE" "runs/$RUN/gh_log"

echo "===== gh-gateway  run=$RUN  node=$(hostname)  job=$SLURM_JOB_ID ====="
echo "cache=$SWEBP_GH_CACHE  budget=$SWEBP_GH_BUDGET  diffs=${DIFFS:-0}  replay=${REPLAY:-0}"

exec "$SWEBP_PYTHON" -m swebp_slurm.gh_gateway serve \
  --run "$RUN" \
  --endpoint-dir "$SWEBP_GH_ENDPOINT_DIR" \
  --cache "$SWEBP_GH_CACHE" \
  --log-dir "runs/$RUN/gh_log" \
  --budget-search "$SWEBP_GH_BUDGET" \
  ${DIFFS:+--diffs} \
  ${REPLAY:+--replay}
