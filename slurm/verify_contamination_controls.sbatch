#!/bin/bash
# Regression check for the two generation contamination controls (config.sh:
# SWEBP_STRIP_HISTORY / SWEBP_BLOCK_GITHUB).
#
# Worth a dedicated job because BOTH mechanisms fail silently. enroot 3.5.0
# accepts and ignores ENROOT_UNSHARE_NET outright (job 2530998 still reached
# GitHub with it set), and an /etc/hosts bind that loses to a later mount leaves
# full egress with no error anywhere. A contaminated run that looks clean is the
# failure mode this whole line of work exists to prevent, so prove both controls
# in both directions against a real image rather than trusting the flags.
#
# Runs the SAME code paths as generation (write_hosts_block / assert_github_blocked
# / strip_future_history from swebp_slurm.run_instance), so it cannot drift from
# what the array actually does. Read-only w.r.t. the shared image store.
#
#   sbatch slurm/verify_contamination_controls.sbatch     # env: SQSH_BASE to pick an image
#SBATCH --job-name=swebp-verify-controls
#SBATCH --account=sci-maalej-swe-bench
#SBATCH --partition=cpu-batch
#SBATCH --constraint=ARCH:X86
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#SBATCH --output=logs/%x-%j.out

set -uo pipefail
cd "${SWEBP_REPO:-${SLURM_SUBMIT_DIR:-$PWD}}"
source ./config.sh
bash slurm/setup_enroot_sysconf.sh >/dev/null

# NodeBB by default: it has a real redis-on-localhost dependency, so it is the
# case where shadowing /etc/hosts could plausibly break the container.
SQSH_BASE="${SQSH_BASE:-nodebb-NodeBB__NodeBB-05f2236193f407cf8e2072757fbd6bb170bc13f0-vf2cf3cbd463b7ad942381f1c6d077626485a1e9e}"
IMG="${IMG:-$SWEBP_IMAGE_STORE/$SQSH_BASE.sqsh}"
[ -r "$IMG" ] || { echo "image not readable: $IMG"; exit 1; }
echo "node=$(hostname)  enroot=$(enroot version)  image=$(basename "$IMG")"

"$SWEBP_PYTHON" - "$IMG" <<'PY'
import sys, tempfile, time
from pathlib import Path

from minisweagent.environments.enroot import EnrootEnvironment
from swebp_slurm.run_instance import (
    assert_github_blocked,
    strip_future_history,
    write_hosts_block,
)

image = sys.argv[1]
failures = []


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}", flush=True)
    if not ok:
        failures.append(label)


def sh(env, cmd):
    return env.execute({"command": cmd})["output"].strip()


def first_line(text):
    return text.splitlines()[0] if text else ""


def make_env(mounts):
    return EnrootEnvironment(image=image, cwd="/app", timeout=1800, mounts=mounts)


print("\n### controls OFF (the contaminated baseline) ###", flush=True)
env = make_env([])
try:
    check("github.com resolves off-loopback", sh(env, "getent hosts github.com").split()[:1] != ["127.0.0.1"],
          first_line(sh(env, "getent hosts github.com")))
    future = sh(env, "git rev-list --all --not HEAD | wc -l")
    check("future commits are reachable", future != "0", f"{future} commits")
finally:
    env.cleanup()

print("\n### controls ON ###", flush=True)
hosts = write_hosts_block(Path(tempfile.mkdtemp()) / "hosts-block")
env = make_env([f"{hosts.resolve()}:/etc/hosts:none:bind,ro"])
try:
    assert_github_blocked(env)
    check("github.com resolves to loopback", True, first_line(sh(env, "getent hosts github.com")))
    check("registry.npmjs.org still resolves", not sh(env, "getent hosts registry.npmjs.org").startswith("127.0.0.1"),
          first_line(sh(env, "getent hosts registry.npmjs.org")))
    check("appending to /etc/hosts is denied",
          "denied" in sh(env, "echo '1.2.3.4 github.com' >> /etc/hosts 2>&1 || echo denied").lower())

    head_before = sh(env, "git rev-parse HEAD")
    t0 = time.time()
    strip_future_history(env)
    check("strip completed", True, f"{time.time() - t0:.1f}s")
    check("no future commits remain", sh(env, "git rev-list --all --not HEAD | wc -l") == "0")
    check("HEAD unchanged", sh(env, "git rev-parse HEAD") == head_before, head_before[:12])
    check("base history intact", int(sh(env, "git log --oneline | wc -l")) > 0)
    check("git diff still works", sh(env, "git diff --stat; echo rc=$?").endswith("rc=0"))
    check("git reset --hard base works", sh(env, f"git reset --hard {head_before} >/dev/null && git rev-parse HEAD") == head_before)
finally:
    env.cleanup()

print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
sys.exit(1 if failures else 0)
PY
rc=$?
echo "verify_contamination_controls rc=$rc"
exit $rc
