"""Single-instance evaluation for SWE-Bench Pro on enroot.

Runs the Pro eval entryscript -- reset to base_commit, ``git apply`` the model
patch, check out the gold test files, run the tests, parse results -- inside the
instance's enroot container, then reports whether the patch *resolves* the
instance: ``(FAIL_TO_PASS | PASS_TO_PASS) <= {PASSED tests}``.

We reuse the Pro fork's own eval logic (``create_entryscript``,
``assemble_workspace_files``, ``strip_binary_hunks``, the workspace I/O) and only
swap the execution backend: instead of Modal/Docker we run the entryscript via
our ``EnrootEnvironment`` with the workspace bind-mounted at ``/workspace``.

Prereqs (same as generation): a compute node with enroot; ENROOT_* storage from
mini's .env (data on node-local /tmp, the /scratch sysconf mirror). No model API
key is needed -- eval calls no LLM -- but the container DOES need host egress
(run_script.sh runs ``npm install`` and starts redis); enroot shares the host
network namespace, so a compute node with egress suffices.
"""

from __future__ import annotations

import ast
import json
import os
from contextlib import contextmanager
from pathlib import Path

import typer
from datasets import load_dataset

# Importing EnrootEnvironment also imports minisweagent, which loads mini's .env
# (ENROOT_* storage paths + ENROOT_SYSCONF_PATH) into the process environment
# before enroot ever runs.
from minisweagent.environments.enroot import EnrootEnvironment

DATASET = "ScaleAI/SWE-bench_Pro"
DEFAULT_IMAGES_DIR = Path("/sc/scratch/conrad.halle/swebp/images")
DEFAULT_DOCKERHUB_USERNAME = "jefzda"

# This package is vendored into the SWE-Bench-Pro repo, so the eval logic and
# the per-instance run_scripts/ + dockerfiles/ sit at the repo root and are
# imported normally. create_entryscript/load_local_script still read those two
# directories by RELATIVE path, so the one call that reaches them runs under
# `_chdir` below rather than a process-global chdir.
import swe_bench_pro_eval as pro

_REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _chdir(path: Path):
    """Temporarily change the working directory, restoring it on exit."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def load_instance(instance_id: str, split: str) -> dict:
    """Load a single instance dict from the Pro dataset by its instance_id.

    The HF dataset has the lowercase ``fail_to_pass`` / ``pass_to_pass`` the eval
    logic expects (the bundled sweap_eval_full_v2.jsonl is UPPERCASE and has
    ``dockerhub_tag=None``), so we source instances from HF.
    """
    for row in load_dataset(DATASET, split=split):
        if row["instance_id"] == instance_id:  # type: ignore[index]
            return row  # type: ignore[return-value]
    raise typer.BadParameter(f"instance_id {instance_id!r} not found in {DATASET}:{split}")


def load_record_from_file(
    instances_file: Path, index: int | None, instance_id: str | None
) -> dict:
    """Load one instance record from a generate_instances JSONL (by line ``--index``
    or by ``instance_id``). Reading a small local file avoids the HF ``load_dataset``
    cache-lock contention that breaks parallel array tasks."""
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


def derive_image_path(sample: dict, images_dir: Path, dockerhub_username: str) -> Path:
    """Default .sqsh path for a sample. Prefers the ``sqsh_base`` baked by
    generate_instances; otherwise derives it from the fork's
    ``get_dockerhub_image_uri`` (matching swe_bench_pro_eval and handling the
    element-web / 128-char-tag edge cases) -- not the dataset's ``dockerhub_tag``."""
    sqsh_base = sample.get("sqsh_base")
    if not sqsh_base:
        image_name = pro.get_dockerhub_image_uri(
            sample["instance_id"], dockerhub_username, sample.get("repo", "")
        )
        tag = image_name.split(":", 1)[1] if ":" in image_name else image_name
        sqsh_base = tag.split(".", 1)[1] if "." in tag else tag
    return images_dir / f"{sqsh_base}.sqsh"


def read_patch(pred_path: Path) -> str:
    """Read the model patch from a .pred file (JSON with model_patch/patch, or
    raw diff text) -- mirroring gather_patches.py."""
    content = pred_path.read_text()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    return data.get("model_patch") or data.get("patch") or ""


def read_patch_from_patches_file(patches_file: Path, instance_id: str) -> str:
    """Read one instance's patch from a combined patches file. Accepts both the
    fork's ``gather_patches.py`` list format (``[{instance_id, patch, prefix}, ...]``)
    and mini's ``preds.json`` dict format (``{iid: {model_patch, ...}}``)."""
    data = json.loads(patches_file.read_text())
    if isinstance(data, list):
        for entry in data:
            if entry.get("instance_id") == instance_id:
                return entry.get("patch") or entry.get("model_patch") or ""
        raise typer.BadParameter(f"instance_id {instance_id!r} not in {patches_file}")
    entry = data.get(instance_id)
    if entry is None:
        raise typer.BadParameter(f"instance_id {instance_id!r} not in {patches_file}")
    if isinstance(entry, dict):
        return entry.get("model_patch") or entry.get("patch") or ""
    return entry


def determine_resolved(sample: dict, output: dict | None) -> tuple[bool, set, set]:
    """Resolved iff every required test (FAIL_TO_PASS + PASS_TO_PASS) PASSED.

    Returns (resolved, required_tests, passed_tests). The dataset stores the test
    lists as Python-literal strings (mixed quotes -> not valid JSON), so use
    ast.literal_eval, as the fork's eval() does.
    """
    if not output or "tests" not in output:
        return False, set(), set()
    passed = {t["name"] for t in output["tests"] if t["status"] == "PASSED"}
    required = set(ast.literal_eval(sample["fail_to_pass"])) | set(ast.literal_eval(sample["pass_to_pass"]))
    return required <= passed, required, passed


# fmt: off
@app.command()
def main(
    instance_id: str | None = typer.Argument(None, help="SWE-Bench Pro instance_id (loaded from HF). With --instances-file, optional; selects by id instead of --index."),
    pred: Path | None = typer.Option(None, "--pred", help="Path to a single .pred file (default: <gen-output>/<iid>/<iid>.pred)", rich_help_panel="Basic"),
    patches_file: Path | None = typer.Option(None, "--patches-file", help="Combined patches.json (gather_patches.py / preds.json); read this instance's patch by id. Takes precedence over --pred.", rich_help_panel="Basic"),
    gen_output: Path = typer.Option(Path("generation_output"), "--gen-output", help="Generation output dir to find the default .pred", rich_help_panel="Basic"),
    image: Path | None = typer.Option(None, "--image", help="Explicit .sqsh path (default: derive <images-dir>/<sqsh_base>.sqsh)", rich_help_panel="Basic"),
    images_dir: Path = typer.Option(DEFAULT_IMAGES_DIR, "--images-dir", help="Directory of staged .sqsh images", rich_help_panel="Advanced"),
    output: Path = typer.Option(Path("eval_output"), "-o", "--output", help="Eval output directory", rich_help_panel="Basic"),
    instances_file: Path | None = typer.Option(None, "--instances-file", help="JSONL from generate_instances; read the instance locally instead of loading the HF dataset", rich_help_panel="Data selection"),
    index: int | None = typer.Option(None, "--index", help="0-based line index into --instances-file (e.g. $SLURM_ARRAY_TASK_ID)", rich_help_panel="Data selection"),
    split: str = typer.Option("test", "--split", help="Dataset split (HF path only)", rich_help_panel="Data selection"),
    dockerhub_username: str = typer.Option(DEFAULT_DOCKERHUB_USERNAME, "--dockerhub-username", help="DockerHub user for image derivation (HF path only)", rich_help_panel="Advanced"),
    prefix: str = typer.Option("eval", "--prefix", help="Filename prefix for this run's eval artifacts", rich_help_panel="Advanced"),
    timeout: int = typer.Option(1800, "--timeout", help="Timeout (s) for the whole entryscript (npm install + tests)", rich_help_panel="Advanced"),
    redo: bool = typer.Option(False, "--redo", help="Re-run even if an output already exists", rich_help_panel="Advanced"),
) -> None:
    # fmt: on
    """Evaluate one SWE-Bench Pro patch inside its enroot container."""
    if instances_file is not None:
        sample = load_record_from_file(instances_file, index, instance_id)
        instance_id = sample["instance_id"]
    else:
        if instance_id is None:
            raise typer.BadParameter("pass an instance_id, or --instances-file with --index")
        sample = load_instance(instance_id, split)

    if patches_file is not None:
        patch = read_patch_from_patches_file(patches_file, instance_id)
        patch_src: Path = patches_file
    else:
        pred_path = (pred or gen_output / instance_id / f"{instance_id}.pred").resolve()
        if not pred_path.exists():
            raise typer.BadParameter(f"pred not found: {pred_path}")
        patch = read_patch(pred_path)
        patch_src = pred_path
    if not patch.strip():
        # An empty patch is a legitimate NOT-RESOLVED, not a harness fault: the
        # agent simply produced nothing (step limit, API outage, refusal). Raising
        # here would exit with typer's usage code 2, which the sbatch wrappers map
        # to HARNESS_ERROR -- making a degraded run indistinguishable from a broken
        # harness, and recording `null` instead of `false` in results.json.
        print(f"Empty patch from {patch_src}: nothing to apply -> NOT RESOLVED")
        raise typer.Exit(code=1)

    image_path = (image or derive_image_path(sample, images_dir, dockerhub_username)).resolve()
    if not image_path.exists():
        raise typer.BadParameter(f"image not found: {image_path}")

    output_dir = output.resolve()

    # create_entryscript/load_local_script resolve dockerfiles/ and run_scripts/
    # relative to the repo root, so the single call that reaches them is wrapped
    # in _chdir below. output_dir/pred/image are already absolute.
    scripts_dir = "run_scripts"

    existing, _output_path, workspace_dir = pro.prepare_run(instance_id, str(output_dir), prefix, redo)
    if existing is not None:
        print(f"Reusing existing eval output for {instance_id} (pass --redo to re-run)")
        output = existing
    else:
        with _chdir(_REPO_ROOT):
            files, entryscript = pro.assemble_workspace_files(instance_id, scripts_dir, patch, sample)
        pro.write_files_local(workspace_dir, files)
        pro.write_patch_snapshot(str(output_dir), instance_id, prefix, patch)

        abs_ws = os.path.abspath(workspace_dir)
        env = EnrootEnvironment(
            image=str(image_path),
            cwd="/app",
            timeout=timeout,
            mounts=[f"{abs_ws}:/workspace:none:x-create=dir,bind,rw"],
        )
        try:
            print(f"Running eval entryscript for {instance_id} in {image_path.name} ...")
            result = env.execute({"command": "bash /workspace/entryscript.sh"}, timeout=timeout)
            print(f"entryscript returncode: {result['returncode']}")
        finally:
            env.cleanup()

        output = pro.collect_outputs_local(workspace_dir, str(output_dir), instance_id, prefix)
        pro.save_entryscript_copy(str(output_dir), instance_id, prefix, entryscript)

    resolved, required, passed = determine_resolved(sample, output)
    n_tests = len(output["tests"]) if output and "tests" in output else 0
    missing = sorted(required - passed)

    # Per-instance verdict is reported below (and recorded in the array's _status
    # shard); the run-wide {iid: resolved} aggregate is produced by summarize.py.
    # (We intentionally do NOT write a shared eval_results.json here -- parallel
    # array tasks would clobber it, leaving only the last task's single entry.)
    print("=" * 60)
    print(f"instance : {instance_id}")
    print(f"tests parsed : {n_tests} | required : {len(required)} | passed(required) : {len(required & passed)}")
    print(f"RESOLVED : {resolved}")
    if not resolved and output is not None:
        print(f"missing/failed required tests ({len(missing)}):")
        for name in missing[:10]:
            print(f"  - {name}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")
    if output is None:
        print("NOTE: no output.json was produced -- check the *_stderr.log / entryscript.")
    print("=" * 60)

    # Exit code convention: 0 = resolved, 1 = ran but not resolved (a real
    # verdict), 2 = harness error (no output.json). Callers/sbatch can treat
    # 0/1 as "eval ran" and only >=2 as a failure.
    raise typer.Exit(code=2 if output is None else (0 if resolved else 1))


if __name__ == "__main__":
    app()
