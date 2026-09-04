#!/bin/bash
# Guaranteed end-of-pipeline scratch sweep: remove every .sqsh this run staged
# (i.e. the sqsh_base of each instance in instances.jsonl) that still exists.
# Submitted --dependency=afterany on the eval array so it ALWAYS runs -- the backstop
# for images that eval didn't delete itself (failed eval task, or eval never ran
# because gather failed). It only touches images named in instances.jsonl, so
# unrelated staged images (e.g. the NodeBB smoke-test or gold images) are left alone.
#
#   sbatch --dependency=afterany:<eval_jobid> slurm/cleanup.sbatch
#SBATCH --job-name=swebp-cleanup
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:15:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
cd /sc/home/conrad.halle/projects/swe-bench-pro-slurm

RUN="${RUN:-runs/default}"
INSTANCES="${INSTANCES:-$RUN/instances.jsonl}"
IMAGES_DIR="${IMAGES_DIR:-/sc/scratch/conrad.halle/swebp/images}"

echo "sweeping images for '$INSTANCES' from '$IMAGES_DIR'"
.venv/bin/python -c "
import json, os
removed = freed = 0
for line in open('$INSTANCES'):
    line = line.strip()
    if not line:
        continue
    p = os.path.join('$IMAGES_DIR', json.loads(line)['sqsh_base'] + '.sqsh')
    if os.path.exists(p):
        freed += os.path.getsize(p)
        os.remove(p)
        removed += 1
print(f'removed {removed} staged images, freed {freed/1e9:.1f} GB')
"
