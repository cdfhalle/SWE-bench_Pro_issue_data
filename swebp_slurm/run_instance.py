"""Single-instance generation entrypoint for SWE-Bench Pro on enroot.

Loads one instance from the ``ScaleAI/SWE-bench_Pro`` dataset, runs
mini-swe-agent (v2.x) inside our :class:`EnrootEnvironment`, and writes the
produced patch as a ``.pred`` in the directory layout that the Pro fork's
``helper_code/gather_patches.py`` consumes::

    <output_dir>/<instance_id>/<instance_id>.pred   (JSON: model_name_or_path,
                                                     instance_id, model_patch)

This is *generation only* -- evaluation is a separate, later step.

Prerequisites (see the project handover for the why):
  * Run on a compute node with node-local scratch (enroot needs user
    namespaces + fast local storage), not the interactive login node.
  * ``ENROOT_DATA_PATH`` / ``ENROOT_RUNTIME_PATH`` must point at node-local
    scratch and ``ENROOT_CACHE_PATH`` at (optionally shared) scratch -- NEVER
    home NFS. Configure these plus the model API key in mini's global config
    ``~/.config/mini-swe-agent/.env``; mini loads it on import, so the values
    are in this process's environment before enroot ever runs (which is exactly
    where enroot reads them -- do NOT pass them as container ``--env``).
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import typer
import yaml
from jinja2 import StrictUndefined, Template

# minisweagent loads ~/.config/mini-swe-agent/.env (API key + ENROOT_* paths)
# on import, so importing it here is what populates os.environ.
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_path
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.utils.log import logger

# Import (don't copy) the Pro fork's problem-statement builder so the prompt we
# generate stays identical to the upstream harness's.
from helper_code.create_problem_statement import create_problem_statement

DATASET = "ScaleAI/SWE-bench_Pro"
# The Pro agent config ships with our mini-swe-agent fork as
# minisweagent/config/benchmarks/swebench_pro.yaml. get_config_path() searches
# that benchmarks/ directory, so a bare name resolves it wherever mini is
# installed; a path to a local file still overrides it.
DEFAULT_CONFIG = Path("swebench_pro.yaml")
DEFAULT_IMAGES_DIR = Path("/sc/scratch/conrad.halle/swebp/images")

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def load_instance(instance_id: str, split: str) -> dict:
    """Load a single instance dict from the Pro dataset by its instance_id."""
    from datasets import load_dataset

    logger.info(f"Loading dataset {DATASET} (split={split}) to find {instance_id}...")
    ds = load_dataset(DATASET, split=split)
    matches = [row for row in ds if row["instance_id"] == instance_id]
    if not matches:
        raise typer.BadParameter(
            f"instance_id {instance_id!r} not found in {DATASET} split {split!r}"
        )
    return matches[0]


def load_record_from_file(
    instances_file: Path, index: int | None, instance_id: str | None
) -> dict:
    """Load one instance record from a JSONL produced by generate_instances.

    Reading a small local file (by line ``--index`` or by ``instance_id``) avoids
    the HF ``load_dataset`` cache-lock contention that breaks parallel array tasks.
    The record's ``problem_statement`` is already the rendered task, and it carries
    ``image_name`` / ``sqsh_base``.
    """
    lines = instances_file.read_text().splitlines()
    if index is not None:
        if not 0 <= index < len(lines):
            raise typer.BadParameter(
                f"--index {index} out of range ({instances_file} has {len(lines)} records)"
            )
        return json.loads(lines[index])
    if instance_id is not None:
        for line in lines:
            record = json.loads(line)
            if record.get("instance_id") == instance_id:
                return record
        raise typer.BadParameter(f"instance_id {instance_id!r} not in {instances_file}")
    raise typer.BadParameter("with --instances-file, pass --index or an instance_id")


def build_config(
    *,
    image: Path,
    model_name: str,
    timeout: int,
    step_limit: int | None,
    cost_limit: float | None,
    config_path: Path,
) -> dict:
    """Load mini's swebench.yaml and layer in our enroot + run overrides."""
    config_path = get_config_path(config_path)
    logger.info(f"Loading agent config from '{config_path}'")
    config = yaml.safe_load(config_path.read_text())

    # The Pro config already pins environment_class (our enroot env) and cwd=/app;
    # only the per-run bits are layered in here.
    env_config = config.setdefault("environment", {})
    env_config["image"] = str(image)
    env_config["timeout"] = timeout

    agent_config = config.setdefault("agent", {})
    if step_limit is not None:
        agent_config["step_limit"] = step_limit
    if cost_limit is not None:
        agent_config["cost_limit"] = cost_limit

    config.setdefault("model", {})["model_name"] = model_name
    return config


def run_setup_command(env, instance: dict) -> None:
    """Run the instance's ``before_repo_set_cmd`` to reset the repo to its base
    state (base_commit + the gold test files). This mirrors mini's
    ``env_startup_command`` step, and is the runner's job because the command is
    a dataset field, not environment mechanics.

    ``EnrootEnvironment.execute`` already runs commands in ``env.config.cwd``
    (it prepends a ``cd`` -- the enroot analog of docker's ``-w``), so we do not
    re-handle the working directory here. We only prepend ``set -e`` so a failing
    git step is surfaced rather than masked by the dataset command's trailing
    newline (which would otherwise let bash exit 0 even if a reset step failed).
    """
    setup = instance.get("before_repo_set_cmd", "").strip()
    if not setup:
        logger.info("No before_repo_set_cmd for this instance; skipping setup.")
        return
    rendered = Template(setup, undefined=StrictUndefined).render(**instance)
    logger.info(f"Running before_repo_set_cmd in {env.config.cwd}...")
    out = env.execute({"command": f"set -e\n{rendered}"}, timeout=600)
    if out["returncode"] != 0:
        raise RuntimeError(
            f"before_repo_set_cmd failed (rc={out['returncode']}):\n{out['output']}"
        )


def write_outputs(
    output_dir: Path,
    instance_id: str,
    model_name: str,
    patch: str,
    agent: DefaultAgent | None,
    exit_status: str | None,
    extra_info: dict | None,
) -> Path:
    """Write the trajectory and the gather_patches-compatible .pred file."""
    instance_dir = output_dir / instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)

    if agent is not None:
        traj_path = instance_dir / f"{instance_id}.traj.json"
        agent.save(
            traj_path,
            {
                "info": {
                    "exit_status": exit_status,
                    "submission": patch,
                    **(extra_info or {}),
                },
                "instance_id": instance_id,
            },
        )
        logger.info(f"Saved trajectory to '{traj_path}'")

    pred_path = instance_dir / f"{instance_id}.pred"
    pred_path.write_text(
        json.dumps(
            {
                "model_name_or_path": output_dir.name,
                "instance_id": instance_id,
                "model_patch": patch,
            },
            indent=2,
        )
    )
    return pred_path


# fmt: off
@app.command()
def main(
    instance_id: str | None = typer.Argument(None, help="SWE-Bench Pro instance_id (loaded from HF). With --instances-file, optional; selects by id instead of --index."),
    image: Path | None = typer.Option(None, "--image", help="Path to the pre-staged .sqsh image (default: derive <images-dir>/<sqsh_base>.sqsh from the instance record)", rich_help_panel="Basic"),
    output: Path = typer.Option(..., "-o", "--output", help="Output directory (results land in <output>/<instance_id>/)", rich_help_panel="Basic"),
    instances_file: Path | None = typer.Option(None, "--instances-file", help="JSONL from generate_instances; read the instance locally instead of loading the HF dataset", rich_help_panel="Data selection"),
    index: int | None = typer.Option(None, "--index", help="0-based line index into --instances-file (e.g. $SLURM_ARRAY_TASK_ID)", rich_help_panel="Data selection"),
    images_dir: Path = typer.Option(DEFAULT_IMAGES_DIR, "--images-dir", help="Directory of staged .sqsh images (used to derive --image when omitted)", rich_help_panel="Advanced"),
    model_name: str = typer.Option("anthropic/claude-sonnet-4-5-20250929", "-m", "--model", help="Model to use", rich_help_panel="Basic"),
    split: str = typer.Option("test", "--split", help="Dataset split (HF path only)", rich_help_panel="Data selection"),
    timeout: int = typer.Option(600, "--timeout", help="Per-command timeout in seconds (agent commands; Node builds can be slow)", rich_help_panel="Advanced"),
    step_limit: int | None = typer.Option(None, "--step-limit", help="Override agent step limit", rich_help_panel="Advanced"),
    cost_limit: float | None = typer.Option(None, "-l", "--cost-limit", help="Override agent cost limit (USD)", rich_help_panel="Advanced"),
    config_path: Path = typer.Option(DEFAULT_CONFIG, "-c", "--config", help="mini config file to base the run on", rich_help_panel="Advanced"),
    setup: bool = typer.Option(True, "--setup/--no-setup", help="Run the instance's before_repo_set_cmd before the agent", rich_help_panel="Advanced"),
) -> None:
    # fmt: on
    """Generate a patch for one SWE-Bench Pro instance with mini + enroot."""
    output.mkdir(parents=True, exist_ok=True)

    if instances_file is not None:
        instance = load_record_from_file(instances_file, index, instance_id)
        instance_id = instance["instance_id"]
        task = instance["problem_statement"]  # already rendered by generate_instances
    else:
        if instance_id is None:
            raise typer.BadParameter("pass an instance_id, or --instances-file with --index")
        instance = load_instance(instance_id, split)
        task = create_problem_statement(instance)

    if image is None:
        sqsh_base = instance.get("sqsh_base")
        if not sqsh_base:
            raise typer.BadParameter("--image is required when the instance record has no sqsh_base")
        image = images_dir / f"{sqsh_base}.sqsh"
    if not image.exists():
        raise typer.BadParameter(f"image not found: {image}")

    config = build_config(
        image=image,
        model_name=model_name,
        timeout=timeout,
        step_limit=step_limit,
        cost_limit=cost_limit,
        config_path=config_path,
    )

    env = get_environment(config["environment"])
    if setup:
        run_setup_command(env, instance)

    agent = DefaultAgent(get_model(config=config.get("model", {})), env, **config.get("agent", {}))

    exit_status: str | None = None
    patch: str = ""
    extra_info: dict | None = None
    try:
        info = agent.run(task)
        exit_status = info.get("exit_status")
        patch = info.get("submission", "") or ""
    except Exception as e:  # noqa: BLE001 -- record and still write outputs
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
        exit_status = type(e).__name__
        extra_info = {"traceback": traceback.format_exc(), "exception_str": str(e)}
    finally:
        pred_path = write_outputs(
            output, instance_id, model_name, patch, agent, exit_status, extra_info
        )

    logger.info(f"Exit status: {exit_status}. Patch length: {len(patch)} chars.")
    if not patch.strip():
        logger.warning(f"Empty patch for {instance_id} -- wrote {pred_path} anyway.")
    else:
        logger.info(f"Wrote patch to {pred_path}")


if __name__ == "__main__":
    app()
