from __future__ import annotations

import argparse
import json
import sys

from helper_code import scoped_github_mcp_server as mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run scoped GitHub MCP tool function directly.")
    parser.add_argument("--query", default="", help="Search query")
    parser.add_argument("--limit", type=int, default=1, help="Result limit")
    args = parser.parse_args()

    try:
        result = mcp_server.search_pull_requests(query=args.query, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        print(f"Error running search_pull_requests: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
