#!/bin/bash
# Build the shared image store's integrity + provenance metadata:
#
#   sha256sums.txt  sha256 of every .sqsh, in `sha256sum -c` format
#   manifest.tsv    instance_id -> canonical DockerHub image -> .sqsh file -> bytes -> sha256
#
# The .sqsh filename is a harness-derived name (the DockerHub tag with the leading
# "<repo_base>." stripped, see swebp_slurm/generate_instances.py), so a consumer cannot
# recover the upstream image from the filename alone -- hence the manifest.
#
#   sbatch slurm/checksum_images.sbatch
#
# Env: STORE (default $SWEBP_IMAGE_STORE), INSTANCES (jsonl providing the mapping).
#SBATCH --job-name=swebp-checksum-images
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=16
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=logs/%x-%j.out

set -euo pipefail
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh

STORE="${STORE:-$SWEBP_IMAGE_STORE}"
INSTANCES="${INSTANCES:-runs/baseline-full/instances.jsonl}"

echo "===== checksum images  node=$(hostname)  $(date -Is) ====="
echo "store=$STORE"
echo "instances=$INSTANCES"
[ -d "$STORE" ] || { echo "store dir missing: $STORE"; exit 1; }

# The store is mode 2750 and owned by the publisher, so metadata is written to a
# temporary file here and installed at the end -- a partial sha256sums.txt would be
# worse than none at all.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "--- hashing $(date -Is)"
cd "$STORE"
find . -maxdepth 1 -name '*.sqsh' -type f -printf '%f\n' | sort \
  | xargs -P 16 -I{} sha256sum {} > "$TMP/sha256sums.txt"
sort -k2 "$TMP/sha256sums.txt" -o "$TMP/sha256sums.txt"
echo "hashed $(wc -l < "$TMP/sha256sums.txt") images"

echo "--- building manifest $(date -Is)"
cd "${SWEBP_REPO:-$SLURM_SUBMIT_DIR}"
"$SWEBP_PYTHON" - "$STORE" "$INSTANCES" "$TMP" <<'PY'
import json, os, sys

store, instances, tmp = sys.argv[1], sys.argv[2], sys.argv[3]

sha = {}
with open(os.path.join(tmp, "sha256sums.txt")) as fh:
    for line in fh:
        digest, name = line.split(maxsplit=1)
        sha[name.strip()] = digest

rows, missing = [], 0
with open(instances) as fh:
    for line in fh:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        fname = r["sqsh_base"] + ".sqsh"
        path = os.path.join(store, fname)
        if not os.path.exists(path):
            missing += 1
            continue
        rows.append((r["instance_id"], r["image_name"], fname,
                     str(os.path.getsize(path)), sha.get(fname, "")))

rows.sort()
with open(os.path.join(tmp, "manifest.tsv"), "w") as out:
    out.write("instance_id\timage_name\tsqsh_file\tbytes\tsha256\n")
    for row in rows:
        out.write("\t".join(row) + "\n")

print(f"manifest rows: {len(rows)}  (in instances.jsonl but not in store: {missing})")
unhashed = [r[2] for r in rows if not r[4]]
if unhashed:
    print(f"WARNING: {len(unhashed)} rows have no sha256, e.g. {unhashed[:3]}")
# Images present in the store but absent from instances.jsonl are not an error --
# the store is allowed to outlive any single run -- but they are worth surfacing.
extra = set(sha) - {r[2] for r in rows}
if extra:
    print(f"note: {len(extra)} store images not in {instances}, e.g. {sorted(extra)[:3]}")
PY

echo "--- installing metadata $(date -Is)"
install -m 0440 "$TMP/sha256sums.txt" "$STORE/sha256sums.txt"
install -m 0440 "$TMP/manifest.tsv"   "$STORE/manifest.tsv"
ls -l "$STORE/sha256sums.txt" "$STORE/manifest.tsv"
echo "===== done $(date -Is)"
