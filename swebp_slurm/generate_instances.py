"""Materialize SWE-Bench Pro instances into a single local file for batch runs.

This mirrors the Pro fork's ``helper_code/generate_sweagent_instances.py`` (which
produces ``SWE-agent/data/instances.yaml`` for SWE-agent's batch runner): we load
the ``ScaleAI/SWE-bench_Pro`` dataset **once** and write one record per instance
with the DockerHub ``image_name`` (via the fork's ``get_dockerhub_image_uri``) and
the rendered ``problem_statement`` (via the fork's ``create_problem_statement``)
*baked in* -- the same shape mini-swe-agent's batch runner consumes
(``instance["image_name"]`` / ``instance["problem_statement"]``).

Two differences from the fork's script, both deliberate:
  * We emit **JSONL** (one object per line), not YAML, so a Slurm array task can
    pick its instance by line index (``--index``) cheaply, and so both the
    generation and evaluation runners can read one instance without calling
    ``datasets.load_dataset`` (which contends on the HF cache lock over NFS when
    many array tasks run in parallel -- the whole reason this file exists).
  * Each record is the **full dataset row** plus ``image_name`` / ``sqsh_base`` and
    with ``problem_statement`` replaced by the rendered task, so the *same* file
    also serves evaluation, whose helpers read ``before_repo_set_cmd`` /
    ``selected_test_files_to_run`` / ``base_commit`` / ``fail_to_pass`` /
    ``pass_to_pass`` from the row (they never read ``problem_statement``, so the
    overwrite is safe).
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

import typer
from minisweagent.utils.log import logger  # importing mini also loads its .env

# Reuse the repo's own helpers so image names and problem statements stay
# identical to the upstream harness's.
from helper_code.create_problem_statement import create_problem_statement
from helper_code.image_uri import get_dockerhub_image_uri
from swebp_slurm import DEFAULT_DOCKERHUB_USERNAME

DATASET = "ScaleAI/SWE-bench_Pro"

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def sqsh_base_from_image_name(image_name: str) -> str:
    """The staged ``.sqsh`` basename for an image_name: the DockerHub *tag* with the
    leading ``<repo_base>.`` stripped (same convention as the staged files and the
    gold manifest, including get_dockerhub_image_uri's 128-char tag truncation)."""
    tag = image_name.split(":", 1)[1] if ":" in image_name else image_name
    return tag.split(".", 1)[1] if "." in tag else tag


def build_record(row: dict, dockerhub_username: str) -> dict:
    """Full dataset row + baked image_name/sqsh_base + rendered problem_statement."""
    instance_id = row["instance_id"]
    image_name = get_dockerhub_image_uri(instance_id, dockerhub_username, row.get("repo", ""))
    record = dict(row)
    record["image_name"] = image_name
    record["sqsh_base"] = sqsh_base_from_image_name(image_name)
    # Bake the rendered task (problem_statement + requirements + interface), as the
    # fork's generate_sweagent_instances.py does. Overwrites the raw problem_statement
    # field, which no downstream consumer reads.
    record["problem_statement"] = create_problem_statement(row)
    return record


def select_instances(
    instances: list[dict],
    *,
    filter_spec: str,
    slice_spec: str,
    shuffle: bool,
    ids: set[str] | None,
) -> list[dict]:
    """Filter/slice/shuffle the instance list (knobs modeled on mini's
    run/benchmarks/swebench.py:filter_instances)."""
    before = len(instances)
    if ids:
        instances = [i for i in instances if i["instance_id"] in ids]
    if shuffle:
        instances = sorted(instances, key=lambda x: x["instance_id"])
        random.seed(42)
        random.shuffle(instances)
    if filter_spec:
        instances = [i for i in instances if re.match(filter_spec, i["instance_id"])]
    if slice_spec:
        bounds = [int(x) if x else None for x in slice_spec.split(":")]
        instances = instances[slice(*bounds)]
    logger.info(f"Selected {len(instances)} / {before} instances")
    return instances


# fmt: off
@app.command()
def main(
    output: Path = typer.Option(Path("instances.jsonl"), "-o", "--output", help="Output JSONL path (one instance record per line)", rich_help_panel="Basic"),
    split: str = typer.Option("test", "--split", help="Dataset split", rich_help_panel="Data selection"),
    slice_spec: str = typer.Option("", "--slice", help="Slice the (filtered/shuffled) list, e.g. '0:100' for the first 100", rich_help_panel="Data selection"),
    filter_spec: str = typer.Option("", "--filter", help="Keep only instance_ids matching this regex", rich_help_panel="Data selection"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle (seed 42) before slicing -- for a representative sample", rich_help_panel="Data selection"),
    ids_file: Path | None = typer.Option(None, "--ids-file", help="File of instance_ids (one per line) to restrict to", rich_help_panel="Data selection"),
    dockerhub_username: str = typer.Option(DEFAULT_DOCKERHUB_USERNAME, "--dockerhub-username", help="DockerHub user for image_name URIs", rich_help_panel="Advanced"),
) -> None:
    # fmt: on
    """Write the selected SWE-Bench Pro instances to a JSONL file for batch runs."""
    from datasets import load_dataset

    ids: set[str] | None = None
    if ids_file is not None:
        ids = {ln.strip() for ln in ids_file.read_text().splitlines() if ln.strip()}

    logger.info(f"Loading dataset {DATASET} (split={split})...")
    instances = list(load_dataset(DATASET, split=split))
    instances = select_instances(
        instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle, ids=ids
    )
    if not instances:
        raise typer.BadParameter("selection is empty; check --slice/--filter/--ids-file")

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w") as f:
        for row in instances:
            f.write(json.dumps(build_record(row, dockerhub_username)) + "\n")
    logger.info(f"Wrote {len(instances)} instance records to {output}")
    # The array size for submit_batch:
    print(len(instances))


if __name__ == "__main__":
    app()
