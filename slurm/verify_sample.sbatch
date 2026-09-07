#!/bin/bash
# Final pre-deletion check: hash a random sample of images on BOTH sides and compare.
#
# The publish job already proved name+size equality for all 731, and checksum_images
# proved the store is internally consistent -- but neither compares CONTENT against the
# scratch originals. This does, and it is the last gate before scratch is freed.
#
#   sbatch slurm/verify_sample.sbatch      # env: N (sample size, default 20)
#SBATCH --job-name=swebp-verify-sample
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86&GLB_SCRATCH
#SBATCH --cpus-per-task=8
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

SRC="${SRC:-$SWEBP_IMAGES_DIR}"
STORE="${STORE:-$SWEBP_IMAGE_STORE}"
N="${N:-20}"

echo "===== verify sample  node=$(hostname)  $(date -Is) ====="
echo "src=$SRC"
echo "store=$STORE"
echo "sample=$N"

# Deterministic shuffle so the sample is reproducible from the log.
mapfile -t SAMPLE < <(find "$SRC" -maxdepth 1 -name '*.sqsh' -type f -printf '%f\n' \
                      | sort | shuf -n "$N" --random-source=<(yes))

fail=0
for f in "${SAMPLE[@]}"; do
  a=$(sha256sum "$SRC/$f"   | cut -d' ' -f1)
  b=$(sha256sum "$STORE/$f" | cut -d' ' -f1)
  # Cross-check the store side against the published manifest too, so a bad
  # sha256sums.txt cannot pass silently.
  m=$(awk -v n="$f" '$2 == n {print $1}' "$STORE/sha256sums.txt")
  if [ "$a" = "$b" ] && [ "$b" = "$m" ]; then
    echo "  OK   ${a:0:16}  $f"
  else
    echo "  FAIL src=${a:0:16} store=${b:0:16} manifest=${m:0:16}  $f"
    fail=$((fail + 1))
  fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "VERDICT: OK - $N/$N sampled images identical on both sides and match sha256sums.txt"
else
  echo "VERDICT: FAIL - $fail of $N mismatched; DO NOT delete the scratch copy"
fi
echo "===== done $(date -Is)"
exit "$fail"
