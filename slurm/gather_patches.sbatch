#!/bin/bash
# Merge the per-instance generation pred shards (<OUT>/<iid>/<iid>.pred) into a
# single combined patches file via the Pro fork's own helper_code/gather_patches.py,
# producing the list-of-{instance_id,patch,prefix} that the eval reads by id.
# Runs as a small dependent job (afterok the gen array). Driven by submit_batch.
#
#   sbatch --dependency=afterok:<gen_jobid> slurm/gather_patches.sbatch
#SBATCH --job-name=swebp-gather
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:15:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
# Locate the checkout and load site settings. An explicit SWEBP_REPO wins;
# otherwise use the directory sbatch was submitted from (the repo root).
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

RUN="${RUN:-runs/default}"
OUT="${OUT:-$RUN/generation_output}"
PATCHES="${PATCHES:-$RUN/patches.json}"
PREFIX="${PREFIX:-gen}"

echo "gathering preds from '$OUT' -> '$PATCHES' (prefix=$PREFIX)"
"$SWEBP_PYTHON" helper_code/gather_patches.py \
  --directory "$OUT" --prefix "$PREFIX" --output "$PATCHES"
rc=$?

echo "--- patches summary ---"
"$SWEBP_PYTHON" -c "
import json
d = json.load(open('$PATCHES'))
n = len(d)
nonempty = sum(1 for e in d if (e.get('patch') or '').strip())
print(f'{n} entries, {nonempty} with a non-empty patch')
" 2>&1 | tail -3
exit $rc
