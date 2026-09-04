"""Submit the auto-chained SWE-Bench Pro batch pipeline to Slurm.

All artifacts for a run live under ``runs/<run>/`` (instances.jsonl, gen/eval shards,
patches.json, results.json). This builds the shared enroot sysconf mirror + DockerHub
credential dirs once, materializes the run's ``instances.jsonl`` (a single HF load,
skipped if the run already has one), then submits the per-instance arrays with the
right dependency edges::

    gen array -> gather -> eval array -> summarize
                                      \\-> cleanup (afterany)

The .sbatch scripts derive every path from the ``RUN`` dir, so this only passes RUN +
MODEL. Run from a login node.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer

REPO = Path(__file__).resolve().parents[1]
SLURM = REPO / "slurm"

app = typer.Typer(add_completion=False)


# Slurm parses #SBATCH directives before the job script runs, so config.sh cannot
# set these from inside -- they have to be command-line flags, which override the
# directives. Only values actually present in the environment are passed, so the
# directives remain the defaults when config.sh has not been sourced.
_SITE_FLAGS = {
    "SWEBP_ACCOUNT": "--account",
    "SWEBP_PARTITION": "--partition",
    "SWEBP_CONSTRAINT": "--constraint",
}


def site_opts() -> list[str]:
    """--account/--partition/--constraint from the environment, if set."""
    return [f"{flag}={os.environ[var]}" for var, flag in _SITE_FLAGS.items() if os.environ.get(var)]


def sbatch(script: str, *opts: str, **exports: str) -> str:
    """Submit slurm/<script>, returning its job id; kwargs become --export vars."""
    cmd = ["sbatch", "--parsable", *site_opts(), *opts]
    exports = {"SWEBP_REPO": str(REPO), **exports}
    cmd.append("--export=ALL," + ",".join(f"{k}={v}" for k, v in exports.items()))
    cmd.append(str(SLURM / script))
    return subprocess.run(cmd, cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()


@app.command()
def main(
    run: str = typer.Option("", "--run", help="Run name; all artifacts go under runs/<run>/ (default: timestamp). Reused if it already exists."),
    slice_spec: str = typer.Option("0:100", "--slice", help="Instance slice (ignored if the run's instances.jsonl exists)"),
    model: str = typer.Option("openrouter/deepseek/deepseek-v4-flash", "--model", help="litellm model id for generation"),
    throttle: int = typer.Option(10, "--throttle", help="Max concurrent array tasks (bounds staged images on scratch)"),
    api_base: str = typer.Option("", "--api-base", help="OpenAI-compatible base URL for a locally-served model, e.g. http://gx32:8000/v1 (use with --model openai/<served-name>). Match --throttle to the server's --max-running-requests."),
    keep_images: bool = typer.Option(False, "--keep-images", help="Keep the staged .sqsh images: eval does not delete its own, and no cleanup job is submitted. Use when re-running a subset whose images are already staged (re-pulling costs DockerHub rate-limit budget)."),
) -> None:
    """Materialize instances and submit the gen -> gather -> eval -> summarize chain."""
    run = run or datetime.now().strftime("run-%Y%m%d-%H%M")
    run_rel = f"runs/{run}"
    (REPO / run_rel).mkdir(parents=True, exist_ok=True)
    (REPO / "logs").mkdir(exist_ok=True)  # Slurm needs the --output dir to exist at submit time.

    # Build the shared sysconf mirror + credential dirs once: array tasks must not race.
    subprocess.run(["bash", str(SLURM / "setup_enroot_sysconf.sh")], cwd=REPO, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, "-m", "swebp_slurm.setup_dockerhub_creds"], cwd=REPO, check=True)

    instances = REPO / run_rel / "instances.jsonl"
    if not instances.exists():
        subprocess.run(
            [sys.executable, "-m", "swebp_slurm.generate_instances", "--slice", slice_spec, "-o", str(instances)],
            cwd=REPO, check=True,
        )
    n = sum(1 for ln in instances.read_text().splitlines() if ln.strip())
    if n == 0:
        raise SystemExit(f"no instances in {instances}")
    array = f"0-{n - 1}%{throttle}"
    print(f"run: {run_rel}   instances: {n}   (array {array})")

    gen = sbatch("gen_array.sbatch", f"--array={array}", RUN=run_rel, MODEL=model,
                 **({"API_BASE": api_base} if api_base else {}))
    gather = sbatch("gather_patches.sbatch", f"--dependency=afterok:{gen}", RUN=run_rel)
    evl = sbatch("eval_array.sbatch", f"--array={array}", f"--dependency=afterok:{gather}", RUN=run_rel,
                 **({"KEEP_IMAGE": "1"} if keep_images else {}))
    summarize = sbatch("summarize.sbatch", f"--dependency=afterok:{evl}", RUN=run_rel)

    chain = f"chain: gen({gen}) -> gather({gather}) -> eval({evl}) -> summarize({summarize})"
    if keep_images:
        print(f"{chain};  no cleanup job (--keep-images)")
    else:
        cleanup = sbatch("cleanup.sbatch", f"--dependency=afterany:{evl}", RUN=run_rel)
        print(f"{chain};  cleanup({cleanup}) afterany eval")
    print(f"watch: squeue --me  |  verdicts: {run_rel}/eval_output/_status/  |  summary: {run_rel}/results.json")


if __name__ == "__main__":
    app()
