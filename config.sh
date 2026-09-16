#!/bin/bash
# Single source of truth for site-specific settings. Every value is
# env-overridable, so nothing here needs editing to run a one-off differently:
#
#   SWEBP_IMAGES_DIR=/somewhere/else sbatch slurm/gen_array.sbatch
#
# Sourced by every slurm/*.sbatch. Keep it side-effect free (no mkdir, no
# network, no enroot) -- it is also sourced by the login-side submitter.

# Repo checkout. Derived from this file's own location so a moved or renamed
# checkout keeps working; override only if you must.
SWEBP_REPO="${SWEBP_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Where staged .sqsh images live. Large (1-6 GB each, one per instance) and on
# scratch, which is NOT backed up and may be purged without warning.
SWEBP_IMAGES_DIR="${SWEBP_IMAGES_DIR:-/sc/scratch/$USER/swebp/images}"

# Read-only shared store of prebuilt images on project storage (backed up, group
# readable). Resolved BEFORE staging from DockerHub, so a run only pulls what the
# store is missing. Set empty to disable and always stage from DockerHub.
#
# It must NEVER equal SWEBP_IMAGES_DIR: the arrays and cleanup.sbatch delete from
# that one, and the store is shared group data. The sbatch scripts assert this.
SWEBP_IMAGE_STORE="${SWEBP_IMAGE_STORE-/sc/projects/sci-maalej/swe-bench/containers/swebench-pro}"

# Slurm submission. Generation and evaluation are CPU-only: the model is reached
# over HTTP, so no GPU is ever requested.
SWEBP_ACCOUNT="${SWEBP_ACCOUNT:-sci-maalej-swe-bench}"
SWEBP_PARTITION="${SWEBP_PARTITION:-cpu-batch}"
SWEBP_CONSTRAINT="${SWEBP_CONSTRAINT:-ARCH:X86}"

# Python for the harness itself (not the container's interpreter).
SWEBP_PYTHON="${SWEBP_PYTHON:-$SWEBP_REPO/.venv/bin/python}"

# Contamination controls for GENERATION. Both default on; set either to 0 to
# reproduce the old, contaminated behaviour for an A/B. Eval is never affected --
# it unpacks the same .sqsh into its own container and still needs the full git
# history for its gold-test checkout.
#
# Why they exist: runs/qwen3827b-full-* scored 587/731 (80.3%) against a published
# 61.7% for the same model. 468 of 731 instances show a contamination signal and
# solve at 87.0%; the 263 with none solve at 68.4%. Both leaks are upstream's
# (scaleapi/SWE-bench_Pro-os#93, open; its PR #94 rebuilds all 731 images, which we
# cannot do -- we import prebuilt jefzda/sweap-images and this cluster has no
# Docker), so we close them at container start instead.
#
# SWEBP_STRIP_HISTORY -- delete refs/reflogs/unreachable objects so the fix commit
#   cannot be read out of the container. 278 instances referenced the fix SHA and
#   solved at 93.9%. Costs 2.9-6.5s per instance and shrinks .git (teleport
#   1.1G -> 96.6M), measured on job 2529619 over four images.
#
# SWEBP_BLOCK_GITHUB -- bind a read-only /etc/hosts pointing github.com and friends
#   at 127.0.0.1, so the fix cannot simply be fetched over HTTP instead. 209
#   instances did exactly that and solved at 87.6%. Verified on job 2530339: clone
#   and API blocked, registry.npmjs.org and proxy.golang.org unaffected.
#   This is resolver-level, NOT isolation. enroot's `--net` would be the right
#   tool but arrived in v4.2.0 and this cluster is pinned at 3.5.0, where
#   ENROOT_UNSHARE_NET is accepted and silently ignored (job 2530998 still reached
#   GitHub with it set). Ask the admins for enroot >= 4.2.0 for the real fix.
SWEBP_STRIP_HISTORY="${SWEBP_STRIP_HISTORY:-1}"
SWEBP_BLOCK_GITHUB="${SWEBP_BLOCK_GITHUB:-1}"

# Endpoint descriptors written by the model-serving repo; read by gen_array to
# discover a self-hosted OpenAI-compatible server without hardcoding a hostname.
# That repo writes one file per model (endpoint-<model_key>.json), so the
# DIRECTORY is the stable thing to point at: gen_array picks the newest
# descriptor reporting ready. SWEBP_ENDPOINT_JSON pins one exact file instead.
#
# Descriptors are per-checkout: each user serves their own model and reads their
# own endpoint/ dir. Look for model-hosting beside this checkout first, then in
# the ~/projects/ layout -- that second candidate is what keeps discovery working
# from a worktree, where "beside this checkout" lands inside .claude/worktrees/
# and does not exist. Set SWEBP_ENDPOINT_DIR to override; if no candidate exists
# discovery finds nothing and the run falls back to --api-base, the same graceful
# degradation as before.
if [ -z "${SWEBP_ENDPOINT_DIR:-}" ]; then
    for _d in "$SWEBP_REPO/../model-hosting/endpoint" "$HOME/projects/model-hosting/endpoint"; do
        [ -d "$_d" ] && { SWEBP_ENDPOINT_DIR="$_d"; break; }
    done
    unset _d
fi
SWEBP_ENDPOINT_DIR="${SWEBP_ENDPOINT_DIR:-$SWEBP_REPO/../model-hosting/endpoint}"
SWEBP_ENDPOINT_JSON="${SWEBP_ENDPOINT_JSON:-}"

export SWEBP_REPO SWEBP_IMAGES_DIR SWEBP_IMAGE_STORE SWEBP_ACCOUNT SWEBP_PARTITION SWEBP_CONSTRAINT \
       SWEBP_PYTHON SWEBP_ENDPOINT_DIR SWEBP_ENDPOINT_JSON \
       SWEBP_STRIP_HISTORY SWEBP_BLOCK_GITHUB
