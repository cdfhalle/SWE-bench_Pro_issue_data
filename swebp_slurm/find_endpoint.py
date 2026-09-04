"""Locate a ready self-hosted model endpoint from its descriptor file(s).

The model-serving repo writes a JSON descriptor per served model
(``endpoint-<model_key>.json``) and removes it when the job exits, so the
directory is the stable thing to point at rather than any one filename.

Prints ``<base_url> <model> <path>`` for the newest descriptor that reports
``ready``, or nothing (exit 1) if there is none -- the caller treats "no output"
as "no endpoint" and falls back to whatever API_BASE it was given.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def find(exact: str = "", directory: str = "") -> tuple[str, str, Path] | None:
    if exact:
        candidates = [Path(exact)]
    elif directory:
        candidates = sorted(
            Path(directory).glob("endpoint*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    else:
        return None
    for path in candidates:
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if d.get("ready") and d.get("base_url"):
            return d["base_url"], str(d.get("model", "?")), path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="", help="Exact descriptor path (wins over --dir)")
    ap.add_argument("--dir", default="", help="Directory of endpoint*.json descriptors")
    args = ap.parse_args()
    found = find(args.file, args.dir)
    if found is None:
        return 1
    base_url, model, path = found
    print(base_url, model, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
