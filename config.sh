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
       SWEBP_PYTHON SWEBP_ENDPOINT_DIR SWEBP_ENDPOINT_JSON
