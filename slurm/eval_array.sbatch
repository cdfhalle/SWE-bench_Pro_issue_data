#!/bin/bash
# Batch evaluation, one array task per instance. Each task reads its instance from
# instances.jsonl (by line = $SLURM_ARRAY_TASK_ID), reads this instance's patch from
# the combined patches.json by id, stages the .sqsh if missing, applies+tests it in
# the enroot container, records a verdict to <OUT>/_status/<task>.txt, and then
# DELETES its .sqsh (it is the last consumer) unless KEEP_IMAGE is set.
#
# Submit (sized to instances.jsonl; %N bounds concurrent images on scratch):
#   bash slurm/setup_enroot_sysconf.sh                 # build the shared mirror ONCE first
#   N=$(wc -l < instances.jsonl)
#   sbatch --dependency=afterok:<gather_jobid> --array=0-$((N-1))%10 slurm/eval_array.sbatch
# Usually driven by submit_batch. Gold sanity-check via --export, e.g.
#   sbatch --export=ALL,PATCHES=gold_patches.json,PREFIX=gold,OUT=eval_output_gold,KEEP_IMAGE=1 ...
#
# Needs host egress (run_script.sh does npm install / go mod / pip and starts redis);
# enroot shares the host net namespace. No model API key required.
#SBATCH --job-name=swebp-eval
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:30:00
#SBATCH --output=logs/%x-%A_%a.out

set -uo pipefail
# Locate the checkout and load site settings. An explicit SWEBP_REPO wins;
# otherwise use the directory sbatch was submitted from (the repo root).
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

# `enroot import` runs in the shell, so export the ENROOT_* paths mini only loads
# into Python from .env.
# (grep keeps only the export lines: importing minisweagent also prints a banner to stdout)
eval "$("$SWEBP_PYTHON" -c 'import minisweagent,os; print("\n".join(f"export {k}={os.environ[k]}" for k in ["ENROOT_CACHE_PATH","ENROOT_DATA_PATH","ENROOT_SYSCONF_PATH"] if k in os.environ))' | grep '^export ')"
mkdir -p "$ENROOT_DATA_PATH" "$ENROOT_CACHE_PATH"

RUN="${RUN:-runs/default}"
INSTANCES="${INSTANCES:-$RUN/instances.jsonl}"
PATCHES="${PATCHES:-$RUN/patches.json}"
OUT="${OUT:-$RUN/eval_output}"
PREFIX="${PREFIX:-eval}"
IMAGES_DIR="${IMAGES_DIR:-$SWEBP_IMAGES_DIR}"
STATUS_DIR="$OUT/_status"
mkdir -p "$IMAGES_DIR" "$STATUS_DIR"

# Authenticated DockerHub staging helper (stage_image; dh1 -> dh2 failover).
source slurm/stage_image.sh

STATUS_FILE="$STATUS_DIR/$SLURM_ARRAY_TASK_ID.txt"
record() { echo "$1 $IID" > "$STATUS_FILE"; echo "VERDICT: $1 $IID"; }

read -r IID IMAGE_NAME SQSH_BASE < <("$SWEBP_PYTHON" -c "
import json
r = json.loads(open('$INSTANCES').read().splitlines()[$SLURM_ARRAY_TASK_ID])
print(r['instance_id'], r['image_name'], r['sqsh_base'])
")
SQSH="$IMAGES_DIR/$SQSH_BASE.sqsh"

echo "===== eval task $SLURM_ARRAY_TASK_ID  node=$(hostname) ====="
echo "iid=$IID"
echo "patches=$PATCHES  prefix=$PREFIX  out=$OUT"
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

"$SWEBP_PYTHON" -m swebp_slurm.run_eval \
  --instances-file "$INSTANCES" --index "$SLURM_ARRAY_TASK_ID" \
  --patches-file "$PATCHES" --image "$SQSH" \
  --prefix "$PREFIX" --output "$OUT" \
  ${REDO:+--redo}
rc=$?
case $rc in
  0) record RESOLVED ;;
  1) record NOT_RESOLVED ;;
  2) record HARNESS_ERROR ;;
  *) record "rc=$rc" ;;
esac

# This task is the last consumer of the image -> remove it ASAP to keep scratch
# clean (KEEP_IMAGE=1 disables, e.g. for gold debugging). cleanup.sbatch is the
# backstop for images whose eval task never ran (e.g. failed gen).
if [ -z "${KEEP_IMAGE:-}" ]; then
  rm -f "$SQSH" && echo "removed staged image $SQSH"
fi
