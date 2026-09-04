#!/bin/bash
# Single-instance generation smoke test: mini-swe-agent + EnrootEnvironment on
# the NodeBB SWE-Bench Pro test instance. Submit from anywhere with:
#
#   sbatch slurm/test_run_instance.sbatch
#
# Requires a compute node with node-local scratch (the SCRATCH:NVME constraint
# is what makes $SLURM_SCRATCH and the /enroot-* mounts available). The model
# API key lives in the gitignored .env (auto-loaded by mini on import).
#SBATCH --job-name=swebp-gen-nodebb
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=02:00:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail

REPO=/sc/home/conrad.halle/projects/swe-bench-pro-slurm
cd "$REPO"

# litellm has no price table for OpenRouter-proxied models, so mini's cost
# calculator raises by default. Tell it to ignore that (cost stays 0; the
# --step-limit, not --cost-limit, is the effective guard for such models).
export MSWEA_COST_TRACKING=ignore_errors

# Rebuild the enroot sysconf mirror that mutes the spurious /scratch mount warning.
bash slurm/setup_enroot_sysconf.sh >/dev/null

IID=instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan
IMG=/sc/scratch/conrad.halle/swebp/images/TEST_nodebb-NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5.sqsh
MODEL=openrouter/deepseek/deepseek-v4-flash
OUT=runs/smoke/generation_output

echo "===== PREFLIGHT ====="
echo "node          : $(hostname)"
echo "job           : ${SLURM_JOB_ID:-?}"
echo "ENROOT_DATA   : $(.venv/bin/python -c 'import minisweagent,os;print(os.environ.get("ENROOT_DATA_PATH"))' 2>/dev/null | tail -1)"
echo "ENROOT_RUNTIME: $(.venv/bin/python -c 'import minisweagent,os;print(os.environ.get("ENROOT_RUNTIME_PATH","<default>"))' 2>/dev/null | tail -1)"
echo -n "openrouter egress: "; curl -sS -o /dev/null -w "%{http_code}\n" --max-time 10 https://openrouter.ai/api/v1/models 2>&1 || echo "FAILED"
echo "image present : $([ -f "$IMG" ] && echo yes || echo NO)"
# enroot's DATA dir is node-local /tmp (per .env); create it on this node.
DATA_DIR=$(.venv/bin/python -c 'import minisweagent,os;print(os.environ.get("ENROOT_DATA_PATH"))' 2>/dev/null | tail -1)
mkdir -p "$DATA_DIR" && echo "ENROOT_DATA dir ready: $DATA_DIR ($(findmnt -no FSTYPE --target "$DATA_DIR"))"
echo "====================="

.venv/bin/python -m swebp_slurm.run_instance "$IID" \
  --image "$IMG" \
  --model "$MODEL" \
  --output "$OUT"
rc=$?

echo "===== RESULT (rc=$rc) ====="
PRED="$OUT/$IID/$IID.pred"
if [ -f "$PRED" ]; then
  echo "pred file: $PRED"
  echo "patch length (chars):"
  .venv/bin/python -c "import json,sys; d=json.load(open('$PRED')); print(len(d.get('model_patch','')))"
  echo "--- patch head ---"
  .venv/bin/python -c "import json; print(json.load(open('$PRED')).get('model_patch','')[:1200])"
else
  echo "NO pred file written at $PRED"
fi
exit $rc
