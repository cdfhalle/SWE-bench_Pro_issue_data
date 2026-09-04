#!/bin/bash
# Shared image-staging helper for the batch arrays. SOURCED (not executed) by
# gen_array.sbatch / eval_array.sbatch after they cd into the repo:  source slurm/stage_image.sh
#
# DockerHub pulls must be authenticated: all cluster nodes egress through one shared NAT
# IP whose anonymous quota (100/6h) is exhausted; authenticated pulls count against the
# account (200/6h) instead. Two enroot 3.5.0 specifics:
#   1. enroot only sends credentials when the registry is EXPLICIT in the URI
#      (docker://registry-1.docker.io#<image>) -- plain docker://jefzda/... pulls anon.
#   2. ${ENROOT_CONFIG_PATH}/.credentials holds ONE account (enroot's awk takes the first
#      matching `machine` line), so each account gets its own config dir
#      (setup_dockerhub_creds) and we pick via ENROOT_CONFIG_PATH at import time.
# stage_image() tries dh1, then fails over to dh2 at its pull cap (~400/6h combined).
DH1_CONFIG="${DH1_CONFIG:-$HOME/.config/enroot-dh1}"
DH2_CONFIG="${DH2_CONFIG:-$HOME/.config/enroot-dh2}"

stage_image() {  # $1=dest .sqsh  $2=image_name (jefzda/sweap-images:<tag>)
  local dest="$1" uri="docker://registry-1.docker.io#$2" cfg
  for cfg in "$DH1_CONFIG" "$DH2_CONFIG"; do
    if ENROOT_CONFIG_PATH="$cfg" enroot import -o "$dest" "$uri"; then return 0; fi
    rm -f "$dest"
    echo "  staging via $(basename "$cfg") failed; trying next DockerHub account"
  done
  return 1
}
