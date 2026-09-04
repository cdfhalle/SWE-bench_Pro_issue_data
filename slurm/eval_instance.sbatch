#!/bin/bash
# Single-instance evaluation: apply the generated patch in the instance's enroot
# container, run the Pro test suite, and report resolved/not. Submit with:
#
#   sbatch slurm/eval_instance.sbatch
#
# Needs a compute node with enroot + host egress (run_script.sh does npm install
# and starts redis; the enroot container shares the host network namespace). No
# model API key required -- eval calls no LLM.
#SBATCH --job-name=swebp-eval-nodebb
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail

# Locate the checkout and load site settings. An explicit SWEBP_REPO wins;
# otherwise use the directory sbatch was submitted from (the repo root).
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh
REPO="$SWEBP_REPO"
cd "$REPO"

# Rebuild the enroot sysconf mirror that mutes the spurious /scratch mount warning.
bash slurm/setup_enroot_sysconf.sh >/dev/null

IID=instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan
# Smoke-test image. The sqsh basename is the DockerHub tag minus the leading
# "<repo_base>." -- stage it first if absent (see slurm/stage_image.sh).
IMG="${IMG:-$SWEBP_IMAGES_DIR/nodebb-NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5.sqsh}"
# Overridable via sbatch --export (e.g. to eval a gold patch):
#   sbatch --export=ALL,PRED=gold_patches/$IID.pred,PREFIX=gold,OUT=runs/smoke/eval_gold ...
OUT="${OUT:-runs/smoke/eval_output}"
PREFIX="${PREFIX:-eval}"
PRED="${PRED:-runs/smoke/generation_output/$IID/$IID.pred}"

echo "===== PREFLIGHT ====="
echo "node          : $(hostname)"
echo "job           : ${SLURM_JOB_ID:-?}"
DATA_DIR=$("$SWEBP_PYTHON" -c 'import minisweagent,os;print(os.environ["ENROOT_DATA_PATH"])' 2>/dev/null | tail -1)
mkdir -p "$DATA_DIR"
echo "ENROOT_DATA   : $DATA_DIR ($(findmnt -no FSTYPE --target "$DATA_DIR"))"
echo -n "npm-registry egress: "; curl -sS -o /dev/null -w "%{http_code}\n" --max-time 10 https://registry.npmjs.org/ 2>&1 || echo "FAILED"
echo "pred          : $PRED ($([ -f "$PRED" ] && echo present || echo MISSING))"
echo "prefix/out    : $PREFIX / $OUT"
echo "image present : $([ -f "$IMG" ] && echo yes || echo NO)"
echo "====================="

"$SWEBP_PYTHON" -m swebp_slurm.run_eval "$IID" \
  --image "$IMG" \
  --pred "$PRED" \
  --prefix "$PREFIX" \
  --output "$OUT" \
  ${REDO:+--redo}
rc=$?

echo "===== RESULT (run_eval rc=$rc; 0=resolved, 1=not resolved, 2=harness error) ====="
echo "--- parsed output.json (test statuses) ---"
"$SWEBP_PYTHON" -c "
import json,glob
f=glob.glob('$OUT/$IID/*_output.json')
if not f: print('no output.json'); raise SystemExit
d=json.load(open(f[0])); ts=d.get('tests',[])
from collections import Counter
print('total tests:', len(ts), '| by status:', dict(Counter(t['status'] for t in ts)))
" 2>&1 | tail -5
# A recorded verdict (resolved=0 / not-resolved=1) is a successful eval run; only
# a harness error (rc>=2) should fail the Slurm job.
[ "$rc" -ge 2 ] && exit "$rc" || exit 0
