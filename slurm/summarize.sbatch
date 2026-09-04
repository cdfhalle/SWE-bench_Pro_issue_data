#!/bin/bash
# Aggregate the batch run into results.json + a printed report (resolved rate,
# status breakdown, per-repo). Runs afterok the eval array. Driven by submit_batch.
#
#   sbatch --dependency=afterok:<eval_jobid> slurm/summarize.sbatch
#SBATCH --job-name=swebp-summarize
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:15:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
cd /sc/home/conrad.halle/projects/swe-bench-pro-slurm

RUN="${RUN:-runs/default}"
INSTANCES="${INSTANCES:-$RUN/instances.jsonl}"
OUT="${OUT:-$RUN/eval_output}"
GEN_OUT="${GEN_OUT:-$RUN/generation_output}"
PREFIX="${PREFIX:-eval}"
RESULTS="${RESULTS:-$RUN/results.json}"

.venv/bin/python -m swebp_slurm.summarize \
  --instances-file "$INSTANCES" \
  --eval-output "$OUT" \
  --gen-output "$GEN_OUT" \
  --prefix "$PREFIX" \
  --results-out "$RESULTS"
