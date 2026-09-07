#!/bin/bash
# Build an enroot "sysconf" mirror with two host-environment fixes, without root.
# enroot reads mounts.d/hooks.d/environ.d from $ENROOT_SYSCONF_PATH (default
# /etc/enroot); the main enroot.conf is read from a hardcoded /etc/enroot path
# regardless. So we mirror /etc/enroot -- mostly symlinks -- with two patched
# fstabs. Point ENROOT_SYSCONF_PATH at this mirror (see .env); it is loaded by
# BOTH generation (run_instance) and evaluation (run_eval), so both share one
# environment.
#
# Fix 1 -- mute the spurious /scratch mount warning:
#   /etc/enroot/mounts.d/30-slurm.fstab bind-mounts the host's /scratch into every
#   container (`/scratch /scratch none x-create=dir,bind,rwx,nofail`). enroot
#   re-applies all fstabs on EVERY `enroot start`, and EnrootEnvironment starts a
#   fresh container per command. cpu-batch nodes have no /scratch (it's provisioned
#   only on SCRATCH:NVME nodes by the Slurm task prolog, which doesn't run when we
#   invoke enroot directly), so the mount fails. It's `nofail` (harmless) but not
#   `silent`, so a warning is printed into every command's output. We add `silent`.
#
# Fix 2 -- make `localhost` resolve to IPv4 (general, all languages):
#   enroot bind-mounts the host's /etc/hosts into the container (unlike Docker,
#   which gives each container its own). The host file maps `::1 localhost`, and
#   with no gai.conf glibc prefers IPv6, so `localhost` -> ::1. Servers in the
#   benchmark images bind 0.0.0.0 (IPv4 only), so any test that connects to a
#   localhost server gets `ECONNREFUSED ::1` and its whole suite (mocha "before
#   all" hook, pytest fixture, etc.) fails. We bind a custom /etc/hosts where
#   `localhost` is IPv4-only, fixing it for every image/runtime.
#
# Fix 3 -- keep the Go build cache off the quota'd home filesystem:
#   The site config (/etc/enroot/enroot.conf.d/enroot-sci.conf) sets
#   ENROOT_MOUNT_HOME=y, so hooks.d/10-home.sh bind-mounts $HOME rw into every
#   container at its host path. Nothing sets GOCACHE, so the 362 Go run_scripts
#   (navidrome, teleport, flipt, vuls) default it to $HOME/.cache/go-build and
#   compile straight onto /sc/home -- 102G in a single full run, against a 200G
#   quota. teleport alone runs `go test -race` over its whole module. We bind a
#   scratch dir in as /gocache and point GOCACHE at it, so the cache lands on
#   GPFS instead. Node-local disk would be faster still, but cpu-batch nodes
#   have no /scratch and the container's /tmp is a tmpfs (RAM), so scratch is
#   the only durable non-home target.
#
# Idempotent. Usage: setup_enroot_sysconf.sh [TARGET_DIR]
set -euo pipefail

SRC=/etc/enroot
TARGET="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/enroot_sysconf}"
# Host dir backing the container's /gocache (Fix 3). Must be absolute and NOT
# under $HOME -- the whole point is to keep it off the home quota.
GOCACHE_HOST="${SWEBP_GOCACHE:-/sc/scratch/$USER/swebp-gocache}"

rm -rf "$TARGET"
mkdir -p "$TARGET/mounts.d" "$TARGET/etc" "$TARGET/environ.d"
TARGET="$(cd "$TARGET" && pwd)"  # ensure absolute (enroot bind sources must be)

# hooks.d: use the real one unchanged. environ.d is a real dir (not a symlink)
# so Fix 3 can drop a file in beside the stock ones, which stay symlinked.
ln -s "$SRC/hooks.d" "$TARGET/hooks.d"
for f in "$SRC"/environ.d/*; do
    [ -e "$f" ] && ln -s "$f" "$TARGET/environ.d/$(basename "$f")"
done

# 10-system: unchanged.
ln -s "$SRC/mounts.d/10-system.fstab" "$TARGET/mounts.d/10-system.fstab"

# Fix 2: IPv4-only-localhost /etc/hosts, bound in place of the host's via a
# patched 20-config.fstab (rewrite only the /etc/hosts mount SOURCE).
cat > "$TARGET/etc/hosts" <<'HOSTS'
127.0.0.1	localhost
::1		ip6-localhost ip6-loopback
ff02::1		ip6-allnodes
ff02::2		ip6-allrouters
HOSTS
sed -E "s#^/etc/hosts([[:space:]]+)/etc/hosts#${TARGET}/etc/hosts\1/etc/hosts#" \
    "$SRC/mounts.d/20-config.fstab" > "$TARGET/mounts.d/20-config.fstab"

# Fix 1: add `silent` to the /scratch line of 30-slurm, copy the rest verbatim.
sed -E '\#^/scratch[[:space:]]#{ /silent/!s/(nofail)/\1,silent/ }' \
    "$SRC/mounts.d/30-slurm.fstab" > "$TARGET/mounts.d/30-slurm.fstab"

# Fix 3: bind the scratch Go cache in as /gocache and point GOCACHE at it.
# Absolute host path, created here so the bind has something to attach to.
mkdir -p "$GOCACHE_HOST"
printf '%s /gocache none x-create=dir,bind,rw,nosuid,nofail,silent 0 -1\n' \
    "$GOCACHE_HOST" > "$TARGET/mounts.d/40-gocache.fstab"
printf 'GOCACHE=/gocache\n' > "$TARGET/environ.d/20-gocache.env"

echo "Built enroot sysconf mirror at: $TARGET"
echo "  /scratch line  : $(grep '^/scratch' "$TARGET/mounts.d/30-slurm.fstab")"
echo "  /etc/hosts mount: $(grep '/etc/hosts' "$TARGET/mounts.d/20-config.fstab" | head -1)"
echo "  GOCACHE        : /gocache -> $GOCACHE_HOST"
