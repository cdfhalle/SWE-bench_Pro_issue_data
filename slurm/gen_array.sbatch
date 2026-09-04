#!/bin/bash
# Batch generation, one array task per instance. Each task reads its instance from
# instances.jsonl (by line = $SLURM_ARRAY_TASK_ID), stages the .sqsh on demand if
# missing, runs mini-swe-agent + EnrootEnvironment, and writes the per-instance
# pred shard to <OUT>/<iid>/<iid>.pred plus a verdict to <OUT>/_status/<task>.txt.
#
# Submit (sized to instances.jsonl; %N throttles concurrent images on scratch):
#   bash slurm/setup_enroot_sysconf.sh                 # build the shared mirror ONCE first
#   N=$(wc -l < instances.jsonl)
#   sbatch --array=0-$((N-1))%10 slurm/gen_array.sbatch
# Usually driven by submit_batch. Override defaults via --export, e.g.
#   sbatch --export=ALL,MODEL=anthropic/claude-sonnet-4-5-20250929,STEP_LIMIT=200 ...
#
# Images are NOT deleted here -- the matching eval task is the last consumer and
# deletes them (slurm/cleanup.sbatch is the guaranteed end-of-pipeline sweep).
#SBATCH --job-name=swebp-gen
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=04:00:00
#SBATCH --output=logs/%x-%A_%a.out

set -uo pipefail
cd /sc/home/conrad.halle/projects/swe-bench-pro-slurm

# litellm has no price table for OpenRouter models; let mini ignore cost calc
# (cost stays 0; --step-limit is the real guard).
export MSWEA_COST_TRACKING=ignore_errors

# `enroot import` runs in the shell, so export the ENROOT_* paths mini only loads
# into Python from .env.
# (grep keeps only the export lines: importing minisweagent also prints a banner to stdout)
eval "$(.venv/bin/python -c 'import minisweagent,os; print("\n".join(f"export {k}={os.environ[k]}" for k in ["ENROOT_CACHE_PATH","ENROOT_DATA_PATH","ENROOT_SYSCONF_PATH"] if k in os.environ))' | grep '^export ')"
mkdir -p "$ENROOT_DATA_PATH" "$ENROOT_CACHE_PATH"

RUN="${RUN:-runs/default}"
INSTANCES="${INSTANCES:-$RUN/instances.jsonl}"
OUT="${OUT:-$RUN/generation_output}"
IMAGES_DIR="${IMAGES_DIR:-/sc/scratch/conrad.halle/swebp/images}"
MODEL="${MODEL:-openrouter/deepseek/deepseek-v4-flash}"

# Local OpenAI-compatible endpoint (our SGLang server). Set API_BASE to route an
# `openai/<served-model-name>` MODEL at it, e.g.
#   --export=ALL,MODEL=openai/deepseek-v4-flash,API_BASE=http://gx32:8000/v1
# litellm reads both vars itself; the key is ignored by the server but must be
# non-empty or litellm raises AuthenticationError before it ever sends a request.
# The host is per-serving-job (gx32 today) -- re-export it for a new server.
if [ -n "${API_BASE:-}" ]; then
  export OPENAI_API_BASE="$API_BASE"
  export OPENAI_API_KEY="${OPENAI_API_KEY:-local}"
  echo "api_base=$OPENAI_API_BASE"
fi

STATUS_DIR="$OUT/_status"
mkdir -p "$IMAGES_DIR" "$STATUS_DIR"

# Authenticated DockerHub staging helper (stage_image; dh1 -> dh2 failover).
source slurm/stage_image.sh

STATUS_FILE="$STATUS_DIR/$SLURM_ARRAY_TASK_ID.txt"
record() { echo "$1 $IID" > "$STATUS_FILE"; echo "VERDICT: $1 $IID"; }

# Pull the three fields this task needs (iid for status, image_name+sqsh_base for
# staging) from its line; run_instance reads the full record itself via --index.
read -r IID IMAGE_NAME SQSH_BASE < <(.venv/bin/python -c "
import json
r = json.loads(open('$INSTANCES').read().splitlines()[$SLURM_ARRAY_TASK_ID])
print(r['instance_id'], r['image_name'], r['sqsh_base'])
")
SQSH="$IMAGES_DIR/$SQSH_BASE.sqsh"

echo "===== gen task $SLURM_ARRAY_TASK_ID  node=$(hostname) ====="
echo "iid=$IID"
echo "model=$MODEL"
echo "sqsh=$SQSH"

if [ -z "$IID" ]; then echo "could not read instance line"; record READ_FAIL; exit 0; fi

if [ ! -f "$SQSH" ]; then
  echo "staging (authenticated): $IMAGE_NAME"
  if ! stage_image "$SQSH" "$IMAGE_NAME"; then
    echo "STAGE FAILED (both DockerHub accounts)"; rm -f "$SQSH"; record STAGE_FAIL; exit 0
  fi
else
  echo "image already staged"
fi

.venv/bin/python -m swebp_slurm.run_instance \
  --instances-file "$INSTANCES" --index "$SLURM_ARRAY_TASK_ID" \
  --image "$SQSH" --model "$MODEL" --output "$OUT" \
  ${STEP_LIMIT:+--step-limit "$STEP_LIMIT"}
rc=$?

PRED="$OUT/$IID/$IID.pred"
if [ "$rc" -ne 0 ]; then
  record HARNESS_ERROR
elif [ ! -f "$PRED" ]; then
  record NO_PRED
else
  plen=$(.venv/bin/python -c "import json;print(len(json.load(open('$PRED')).get('model_patch','').strip()))" 2>/dev/null || echo 0)
  [ "$plen" -gt 0 ] && record SUBMITTED || record EMPTY_PATCH
fi
