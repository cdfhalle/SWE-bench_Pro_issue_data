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
# Locate the checkout and load site settings. An explicit SWEBP_REPO wins;
# otherwise use the directory sbatch was submitted from (the repo root).
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

# litellm has no price table for OpenRouter models; let mini ignore cost calc
# (cost stays 0; --step-limit is the real guard).
export MSWEA_COST_TRACKING=ignore_errors

# `enroot import` runs in the shell, so export the ENROOT_* paths mini only loads
# into Python from .env.
# (grep keeps only the export lines: importing minisweagent also prints a banner to stdout)
eval "$("$SWEBP_PYTHON" -c 'import minisweagent,os; print("\n".join(f"export {k}={os.environ[k]}" for k in ["ENROOT_CACHE_PATH","ENROOT_DATA_PATH","ENROOT_SYSCONF_PATH"] if k in os.environ))' | grep '^export ')"
mkdir -p "$ENROOT_DATA_PATH" "$ENROOT_CACHE_PATH"

RUN="${RUN:-runs/default}"
INSTANCES="${INSTANCES:-$RUN/instances.jsonl}"
OUT="${OUT:-$RUN/generation_output}"
IMAGES_DIR="${IMAGES_DIR:-$SWEBP_IMAGES_DIR}"
MODEL="${MODEL:-openrouter/deepseek/deepseek-v4-flash}"

# Local OpenAI-compatible endpoint (e.g. a self-hosted SGLang server). Set
# API_BASE to route an `openai/<served-model-name>` MODEL at it:
#   --export=ALL,MODEL=openai/deepseek-v4-flash,API_BASE=http://gx24:8000/v1
# litellm reads OPENAI_API_BASE/OPENAI_API_KEY itself; the key is ignored by the
# server but must be non-empty or litellm raises AuthenticationError before it
# ever sends a request.
#
# The serving job runs on a different node each time, so rather than hardcoding a
# hostname we read the endpoint descriptor the serving repo writes (and deletes on
# exit). ENDPOINT_JSON=<path> overrides; an explicit API_BASE wins over both.
ENDPOINT_JSON="${ENDPOINT_JSON:-${SWEBP_ENDPOINT_JSON:-}}"
ENDPOINT_DIR="${ENDPOINT_DIR:-${SWEBP_ENDPOINT_DIR:-}}"
if [ -z "${API_BASE:-}" ] && { [ -n "$ENDPOINT_JSON" ] || [ -n "$ENDPOINT_DIR" ]; }; then
  # The serving repo writes one descriptor per model (endpoint-<key>.json), so
  # scan the directory and take the newest that reports ready. An exact file
  # passed as ENDPOINT_JSON wins outright.
  read -r API_BASE SERVED_MODEL ENDPOINT_SRC < <("$SWEBP_PYTHON" "$SWEBP_REPO/swebp_slurm/find_endpoint.py" \
        --file "$ENDPOINT_JSON" --dir "$ENDPOINT_DIR" 2>/dev/null)
  if [ -n "${API_BASE:-}" ]; then
    echo "discovered endpoint from $ENDPOINT_SRC: $API_BASE (model=$SERVED_MODEL)"
  fi
fi

if [ -n "${API_BASE:-}" ]; then
  export OPENAI_API_BASE="$API_BASE"
  export OPENAI_API_KEY="${OPENAI_API_KEY:-local}"
  echo "api_base=$OPENAI_API_BASE"

  # Fail fast if the server is gone. Without this, every task burns its full
  # litellm retry ladder (4s..60s x7) against a dead host and then reports a
  # silent EMPTY_PATCH -- which is how a mid-run server crash cost most of a
  # batch: the run looked like a bad model rather than a dead endpoint.
  # SKIP_ENDPOINT_CHECK=1 bypasses (e.g. a server without /v1/models).
  # Retry before giving up. A task that starts during a momentary server hiccup
  # -- sglang busy, or the serving job being resubmitted -- would otherwise exit 2
  # and, because gather depends on the array with afterok, cancel the rest of the
  # pipeline. On a 731-instance run that discards hours of completed generation
  # for a blip. Poll for ENDPOINT_WAIT_SECS (default 10 min) instead; a genuinely
  # dead endpoint still fails the chain, which is what should happen.
  if [ -z "${SKIP_ENDPOINT_CHECK:-}" ]; then
    deadline=$(( SECONDS + ${ENDPOINT_WAIT_SECS:-600} ))
    while : ; do
      code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 20 "$OPENAI_API_BASE/models" \
               -H "Authorization: Bearer $OPENAI_API_KEY" 2>/dev/null)
      code=${code:-000}
      echo "endpoint preflight: GET $OPENAI_API_BASE/models -> $code"
      [ "$code" = "200" ] && break
      if [ "$SECONDS" -ge "$deadline" ]; then
        echo "VERDICT: ENDPOINT_DOWN ${IID:-task-$SLURM_ARRAY_TASK_ID}"
        mkdir -p "${OUT:-$RUN/generation_output}/_status"
        echo "ENDPOINT_DOWN unknown" > "${OUT:-$RUN/generation_output}/_status/$SLURM_ARRAY_TASK_ID.txt"
        exit 2
      fi
      sleep 30
    done
  fi
fi

STATUS_DIR="$OUT/_status"
mkdir -p "$IMAGES_DIR" "$STATUS_DIR"

# Authenticated DockerHub staging helper (stage_image; dh1 -> dh2 failover).
source slurm/stage_image.sh

STATUS_FILE="$STATUS_DIR/$SLURM_ARRAY_TASK_ID.txt"
record() { echo "$1 $IID" > "$STATUS_FILE"; echo "VERDICT: $1 $IID"; }

# Pull the three fields this task needs (iid for status, image_name+sqsh_base for
# staging) from its line; run_instance reads the full record itself via --index.
read -r IID IMAGE_NAME SQSH_BASE < <("$SWEBP_PYTHON" -c "
import json
r = json.loads(open('$INSTANCES').read().splitlines()[$SLURM_ARRAY_TASK_ID])
print(r['instance_id'], r['image_name'], r['sqsh_base'])
")
# Prefer the group's shared prebuilt store; only pull from DockerHub on a miss.
# FROM_STORE is non-empty for a store hit, which makes the image undeletable below.
if SQSH="$(store_image "$SQSH_BASE")"; then
  FROM_STORE=1
else
  SQSH="$IMAGES_DIR/$SQSH_BASE.sqsh"; FROM_STORE=
fi

echo "===== gen task $SLURM_ARRAY_TASK_ID  node=$(hostname) ====="
echo "iid=$IID"
echo "model=$MODEL"
echo "sqsh=$SQSH"

if [ -z "$IID" ]; then echo "could not read instance line"; record READ_FAIL; exit 0; fi

if [ -n "$FROM_STORE" ]; then
  echo "image from shared store (no DockerHub pull)"
elif [ ! -f "$SQSH" ]; then
  echo "staging (authenticated): $IMAGE_NAME"
  if ! stage_image "$SQSH" "$IMAGE_NAME"; then
    echo "STAGE FAILED (both DockerHub accounts)"; rm -f "$SQSH"; record STAGE_FAIL; exit 0
  fi
else
  echo "image already staged"
fi

"$SWEBP_PYTHON" -m swebp_slurm.run_instance \
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
  plen=$("$SWEBP_PYTHON" -c "import json;print(len(json.load(open('$PRED')).get('model_patch','').strip()))" 2>/dev/null || echo 0)
  if [ "$plen" -gt 0 ]; then
    record SUBMITTED
  else
    # An empty patch means either the model produced nothing, or the server died
    # mid-episode and every request failed. Those need different responses -- one
    # is a result, the other invalidates the run -- and they are indistinguishable
    # after the fact, because the start-of-job preflight passed in both cases.
    # Re-probe now: if the endpoint has since gone away, say so.
    if [ -n "${OPENAI_API_BASE:-}" ] && [ -z "${SKIP_ENDPOINT_CHECK:-}" ]; then
      code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 20 "$OPENAI_API_BASE/models" \
               -H "Authorization: Bearer ${OPENAI_API_KEY:-local}" 2>/dev/null)
      code=${code:-000}
      if [ "$code" != "200" ]; then
        echo "endpoint postflight: GET $OPENAI_API_BASE/models -> $code (was 200 at job start)"
        record ENDPOINT_DOWN
        exit 0
      fi
    fi
    record EMPTY_PATCH
  fi
fi
