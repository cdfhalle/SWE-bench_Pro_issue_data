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

# Slurm submission. Generation and evaluation are CPU-only: the model is reached
# over HTTP, so no GPU is ever requested.
SWEBP_ACCOUNT="${SWEBP_ACCOUNT:-sci-maalej-swe-bench}"
SWEBP_PARTITION="${SWEBP_PARTITION:-cpu-batch}"
SWEBP_CONSTRAINT="${SWEBP_CONSTRAINT:-ARCH:X86}"

# Python for the harness itself (not the container's interpreter).
SWEBP_PYTHON="${SWEBP_PYTHON:-$SWEBP_REPO/.venv/bin/python}"

# Endpoint descriptor written by the model-serving repo; read by gen_array to
# discover a self-hosted OpenAI-compatible server without hardcoding a hostname.
SWEBP_ENDPOINT_JSON="${SWEBP_ENDPOINT_JSON:-$HOME/projects/model-hosting/endpoint/endpoint.json}"

export SWEBP_REPO SWEBP_IMAGES_DIR SWEBP_ACCOUNT SWEBP_PARTITION SWEBP_CONSTRAINT \
       SWEBP_PYTHON SWEBP_ENDPOINT_JSON
