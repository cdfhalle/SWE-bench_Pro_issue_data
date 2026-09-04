"""Aggregate a batch run into a combined results file + a printed report.

Sources of truth (race-free across the Slurm array):
  * per-instance ``<eval-output>/<iid>/<prefix>_output.json`` -- the parsed test
    statuses; resolved is recomputed here exactly as ``run_eval.determine_resolved``
    does (``(FAIL_TO_PASS | PASS_TO_PASS) <= PASSED``).
  * per-task ``<eval-output>/_status/<task>.txt`` shards (``VERDICT IID``) -- the
    verdict each array task recorded, which also captures instances with no
    output.json (HARNESS_ERROR / STAGE_FAIL / ...).

Writes ``results.json`` (``{iid: resolved_bool_or_null}``) and prints resolved rate,
status breakdown, and a per-repo table. Pure stdlib so it can run anywhere.
"""

from __future__ import annotations

import ast
import json
from collections import Counter, defaultdict
from pathlib import Path

import typer

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


def load_samples(instances_file: Path) -> dict[str, dict]:
    samples = {}
    for line in instances_file.read_text().splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            samples[r["instance_id"]] = r
    return samples


def resolved_from_output(sample: dict, output: dict | None) -> bool | None:
    """True/False resolved, or None if no parsed tests are available."""
    if not output or "tests" not in output:
        return None
    passed = {t["name"] for t in output["tests"] if t["status"] == "PASSED"}
    required = set(ast.literal_eval(sample["fail_to_pass"])) | set(
        ast.literal_eval(sample["pass_to_pass"])
    )
    return required <= passed


def read_status_dir(status_dir: Path) -> dict[str, str]:
    """{iid: verdict} from the per-task `VERDICT IID` shards."""
    verdicts: dict[str, str] = {}
    if not status_dir.is_dir():
        return verdicts
    for f in status_dir.glob("*.txt"):
        parts = f.read_text().split()
        if len(parts) >= 2:
            verdicts[parts[1]] = parts[0]
    return verdicts


# fmt: off
@app.command()
def main(
    instances_file: Path = typer.Option(Path("instances.jsonl"), "--instances-file", help="The run's instances.jsonl (provides the samples)", rich_help_panel="Basic"),
    eval_output: Path = typer.Option(Path("eval_output"), "--eval-output", help="Eval output dir (with <iid>/ and _status/)", rich_help_panel="Basic"),
    prefix: str = typer.Option("eval", "--prefix", help="Eval artifact prefix (matches eval_array.sbatch)", rich_help_panel="Basic"),
    gen_output: Path | None = typer.Option(None, "--gen-output", help="Generation output dir, to also report gen status shards", rich_help_panel="Advanced"),
    results_out: Path = typer.Option(Path("results.json"), "--results-out", help="Where to write {iid: resolved} JSON", rich_help_panel="Basic"),
) -> None:
    # fmt: on
    """Summarize a batch generation+evaluation run."""
    samples = load_samples(instances_file)
    eval_status = read_status_dir(eval_output / "_status")

    results: dict[str, bool | None] = {}
    per_repo: dict[str, list[bool | None]] = defaultdict(list)
    for iid, sample in samples.items():
        out_path = eval_output / iid / f"{prefix}_output.json"
        output = json.loads(out_path.read_text()) if out_path.exists() else None
        resolved = resolved_from_output(sample, output)
        if resolved is None and eval_status.get(iid) == "NOT_RESOLVED":
            # No output.json, but the task recorded a definite verdict -- an empty
            # patch, which never reaches the test suite. That is a real `false`,
            # not a missing measurement; leaving it None would understate the
            # denominator and make a degraded run look like a broken harness.
            resolved = False
        results[iid] = resolved
        per_repo[sample.get("repo", "?")].append(resolved)

    results_out.write_text(json.dumps(results, indent=2))

    total = len(samples)
    resolved_n = sum(1 for v in results.values() if v is True)
    no_verdict = sum(1 for v in results.values() if v is None)
    rate = (resolved_n / total * 100) if total else 0.0

    print("=" * 64)
    print(f"instances           : {total}")
    print(f"RESOLVED            : {resolved_n}  ({rate:.1f}%)")
    print(f"not resolved        : {total - resolved_n - no_verdict}")
    print(f"no parsed output    : {no_verdict}")
    print(f"\neval status shards  : {dict(Counter(eval_status.values()))}")
    down = 0
    if gen_output is not None:
        gen_status = read_status_dir(gen_output / "_status")
        print(f"gen status shards   : {dict(Counter(gen_status.values()))}")
        down = sum(1 for v in gen_status.values() if v == "ENDPOINT_DOWN")
    print("\nper-repo (resolved / total):")
    for repo in sorted(per_repo):
        verdicts = per_repo[repo]
        print(f"  {repo:<40} {sum(1 for v in verdicts if v is True)}/{len(verdicts)}")
    if down:
        # Not a model result: these instances never got an answer, so the resolved
        # rate above is computed over a denominator that includes them. Rerun them
        # once the endpoint is back rather than reading this as a score.
        print()
        print("!" * 64)
        print(f"WARNING: {down}/{total} instances recorded ENDPOINT_DOWN -- the model")
        print("server went away mid-run, so those are not model failures and the")
        print("resolved rate above understates the model. Rerun them.")
        print("!" * 64)

    print("=" * 64)
    print(f"wrote {results_out}")


if __name__ == "__main__":
    app()
