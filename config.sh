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
SWEBP_ENDPOINT_DIR="${SWEBP_ENDPOINT_DIR:-$HOME/projects/model-hosting/endpoint}"
SWEBP_ENDPOINT_JSON="${SWEBP_ENDPOINT_JSON:-}"

# --- GitHub issue-tracker experiment (opt-in; unset => baseline behaviour) ---
# One gh-gateway serves a whole run: it holds the token, applies the
# same-repo/pre-cutoff filters, and is the single place the API rate limits are
# accounted for. That last part is why it is a service and not per-task code --
# the Search API allows 30 requests/minute per token, which a 10-wide array
# would exhaust immediately. Discovery mirrors SWEBP_ENDPOINT_*: point at the
# DIRECTORY of gh-gateway-<run>.json descriptors, or pin SWEBP_GH_URL.
SWEBP_GH_ENDPOINT_DIR="${SWEBP_GH_ENDPOINT_DIR:-$SWEBP_REPO/endpoint}"
SWEBP_GH_URL="${SWEBP_GH_URL:-}"

# Upstream response cache: every GitHub reply is stored by URL, so a rerun is
# served from here and `gh_gateway serve --replay` never touches the network at
# all. On project storage because it IS the reproducibility record for a
# published result -- scratch may be purged without warning.
SWEBP_GH_CACHE="${SWEBP_GH_CACHE:-/sc/projects/sci-maalej/swe-bench/gh-cache}"

# Node-local, and tiny: only the per-instance /etc/hosts overlay lives here.
SWEBP_GH_SCRATCH="${SWEBP_GH_SCRATCH:-/tmp/$USER/swebp-gh}"

# Tracker searches an agent may make per instance. As much experimental hygiene
# as protection: an agent that burns the whole budget is a finding, not a bug.
SWEBP_GH_BUDGET="${SWEBP_GH_BUDGET:-40}"

# Hosts null-routed inside the container, to stop an agent going around the
# gateway. Deliberately only the API host: enroot shares the host network
# namespace, and blocking github.com wholesale would also break `npm install` /
# `go get` of dependencies hosted there -- which would change the environment
# for the treatment arm only and confound the A/B.
SWEBP_GH_BLOCK_HOSTS="${SWEBP_GH_BLOCK_HOSTS:-api.github.com}"

export SWEBP_REPO SWEBP_IMAGES_DIR SWEBP_IMAGE_STORE SWEBP_ACCOUNT SWEBP_PARTITION SWEBP_CONSTRAINT \
       SWEBP_PYTHON SWEBP_ENDPOINT_DIR SWEBP_ENDPOINT_JSON \
       SWEBP_GH_ENDPOINT_DIR SWEBP_GH_URL SWEBP_GH_CACHE SWEBP_GH_SCRATCH SWEBP_GH_BUDGET \
       SWEBP_GH_BLOCK_HOSTS
