#!/bin/bash
# Publish the staged SWE-Bench-Pro .sqsh images from global scratch to the group's
# read-only shared image store on project storage.
#
# Scratch is temporary, not backed up, and may be purged without warning; the store
# lives on backed-up project storage and is readable by the whole sci-maalej-swe-bench
# group so nobody has to re-pull from DockerHub against the ~400/6h cap.
#
# The store dir is mode 2750 owned by the publisher: group gets r-x, NOT write, so a
# stray run with IMAGES_DIR pointed at it cannot delete the group's cache.
#
#   sbatch slurm/publish_images.sbatch
#
# Env: SRC (default $SWEBP_IMAGES_DIR), STORE (default $SWEBP_IMAGE_STORE).
#SBATCH --job-name=swebp-publish-images
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86&GLB_SCRATCH
#SBATCH --cpus-per-task=8
#SBATCH --mem=8G
#SBATCH --time=08:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

SRC="${SRC:-$SWEBP_IMAGES_DIR}"
STORE="${STORE:-$SWEBP_IMAGE_STORE}"

echo "===== publish images  node=$(hostname)  $(date -Is) ====="
echo "src=$SRC"
echo "store=$STORE"

[ -d "$SRC" ]   || { echo "source dir missing: $SRC"; exit 1; }
[ -d "$STORE" ] || { echo "store dir missing (create it first): $STORE"; exit 1; }
# Refuse the degenerate case outright -- rsyncing a dir onto itself would be a no-op
# at best and is always a sign the caller has the wiring wrong.
if [ "$(readlink -f "$SRC")" = "$(readlink -f "$STORE")" ]; then
  echo "REFUSING: SRC and STORE are the same directory"; exit 1
fi

n_src=$(find "$SRC" -maxdepth 1 -name '*.sqsh' -type f | wc -l)
echo "source images: $n_src"
[ "$n_src" -gt 0 ] || { echo "nothing to publish"; exit 1; }

# One rsync per image, 8 at a time. Per-file overhead is irrelevant next to a ~2 GB
# median payload, and this keeps the GPFS->NFS pipe busy without the bookkeeping of
# splitting a --files-from list. -rlt (not -a) omits -p/-o/-g deliberately: --chmod
# sets the mode (sources are 0600) and the setgid store dir sets the group.
echo "--- copying $(date -Is)"
find "$SRC" -maxdepth 1 -name '*.sqsh' -type f -printf '%f\n' \
  | xargs -P 8 -I{} rsync -lt --chmod=F0440 "$SRC/{}" "$STORE/{}"

echo "--- verifying $(date -Is)"
n_dst=$(find "$STORE" -maxdepth 1 -name '*.sqsh' -type f | wc -l)
b_src=$(find "$SRC"   -maxdepth 1 -name '*.sqsh' -type f -printf '%s\n' | paste -sd+ | bc)
b_dst=$(find "$STORE" -maxdepth 1 -name '*.sqsh' -type f -printf '%s\n' | paste -sd+ | bc)
echo "count : src=$n_src dst=$n_dst"
echo "bytes : src=$b_src dst=$b_dst"

# Per-file size comparison -- cheap and authoritative against truncated transfers.
find "$SRC"   -maxdepth 1 -name '*.sqsh' -type f -printf '%f %s\n' | sort > /tmp/pub_src.$$
find "$STORE" -maxdepth 1 -name '*.sqsh' -type f -printf '%f %s\n' | sort > /tmp/pub_dst.$$
if diff -q /tmp/pub_src.$$ /tmp/pub_dst.$$ >/dev/null; then
  echo "VERDICT: OK - all $n_src images present with matching sizes"
  rc=0
else
  echo "VERDICT: MISMATCH - name/size differences follow (src < / dst >)"
  diff /tmp/pub_src.$$ /tmp/pub_dst.$$ | head -50
  rc=1
fi
rm -f /tmp/pub_src.$$ /tmp/pub_dst.$$
echo "===== done $(date -Is) rc=$rc"
exit $rc
