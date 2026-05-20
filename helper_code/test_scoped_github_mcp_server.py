from __future__ import annotations

import importlib
import sys
import types
import unittest


class _FakeFastMCP:
    def __init__(self, _name: str) -> None:
        self._tools: dict[str, object] = {}

    def tool(self):  # noqa: ANN201
        def decorator(func):  # noqa: ANN001, ANN202
            self._tools[func.__name__] = func
            return func

        return decorator

    def run(self) -> None:
        return


def _import_module():
    fake_mcp_module = types.ModuleType("mcp")
    fake_server_module = types.ModuleType("mcp.server")
    fake_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp_module.FastMCP = _FakeFastMCP
    sys.modules["mcp"] = fake_mcp_module
    sys.modules["mcp.server"] = fake_server_module
    sys.modules["mcp.server.fastmcp"] = fake_fastmcp_module
    return importlib.import_module("helper_code.scoped_github_mcp_server")


class FakeGitHubScopedClient:
    def _request(self, path, *, params=None, accept="application/vnd.github+json"):  # noqa: ANN001, ARG002
        if path == "/repos/o/r/commits/base":
            return {"commit": {"committer": {"date": "2024-01-01T00:00:00Z"}}}
        if path == "/search/issues":
            q = (params or {}).get("q", "")
            page = int((params or {}).get("page", 1))
            if page > 1:
                return {"total_count": 0, "items": []}
            if "type:issue" in q:
                return {
                    "total_count": 1,
                    "items": [
                        {
                            "number": 9,
                            "title": "Old bug",
                            "body": "Old parser issue",
                            "state": "closed",
                            "created_at": "2023-12-31T00:00:00Z",
                            "updated_at": "2024-01-01T00:00:00Z",
                        }
                    ],
                }
            if "type:pr" in q:
                return {
                    "total_count": 1,
                    "items": [
                        {
                            "number": 5,
                            "title": "Fix parser edge case",
                            "body": "Initial PR description",
                            "state": "closed",
                            "created_at": "2023-12-30T00:00:00Z",
                            "updated_at": "2023-12-31T00:00:00Z",
                        }
                    ],
                }
            return {"total_count": 0, "items": []}
        if path == "/repos/o/r/issues/11":
            return {"number": 11, "title": "Fix parser crash", "created_at": "2024-01-05T00:00:00Z"}
        if path == "/repos/o/r/issues/9":
            return {
                "number": 9,
                "title": "Old bug",
                "body": "Old parser issue",
                "state": "closed",
                "user": {"login": "alice"},
                "labels": [{"name": "bug"}],
                "created_at": "2023-12-31T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        if path == "/repos/o/r/issues/9/comments":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return [
                    {
                        "id": 901,
                        "user": {"login": "bob"},
                        "created_at": "2023-12-31T12:00:00Z",
                        "updated_at": "2023-12-31T12:01:00Z",
                        "body": "I can reproduce this parser issue.",
                    },
                    {
                        "id": 902,
                        "user": {"login": "charlie"},
                        "created_at": "2024-01-02T00:00:00Z",
                        "updated_at": "2024-01-02T00:01:00Z",
                        "body": "New comment after cutoff.",
                    },
                ]
            return []
        if path == "/repos/o/r/issues/comments/777":
            return {
                "id": 777,
                "issue_url": "https://api.github.com/repos/o/r/issues/9",
                "user": {"login": "bob"},
                "created_at": "2023-12-31T08:00:00Z",
                "updated_at": "2023-12-31T08:30:00Z",
                "body": "Relevant parser comment.",
            }
        if path == "/repos/o/r/pulls/5":
            return {
                "number": 5,
                "title": "Fix parser edge case",
                "body": "Initial PR description",
                "state": "closed",
                "user": {"login": "maintainer"},
                "created_at": "2023-12-30T00:00:00Z",
                "updated_at": "2023-12-31T00:00:00Z",
            }
        if path == "/repos/o/r/issues/5/comments":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return [
                    {
                        "id": 801,
                        "user": {"login": "reviewer"},
                        "created_at": "2023-12-30T01:00:00Z",
                        "updated_at": "2023-12-30T01:00:00Z",
                        "body": "Looks good overall.",
                    }
                ]
            return []
        if path == "/repos/o/r/pulls/5/comments":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return [
                    {
                        "id": 811,
                        "user": {"login": "reviewer"},
                        "created_at": "2023-12-30T02:00:00Z",
                        "updated_at": "2023-12-30T02:00:00Z",
                        "body": "Nit: rename variable.",
                    }
                ]
            return []
        if path == "/repos/o/r/pulls/5/commits":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return [
                    {
                        "sha": "c1",
                        "commit": {
                            "message": "fix parser whitespace handling",
                            "author": {"name": "maintainer", "date": "2023-12-30T03:00:00Z"},
                            "committer": {"date": "2023-12-30T03:00:00Z"},
                        },
                    }
                ]
            return []
        if path == "/repos/o/r/commits/old":
            return {
                "sha": "old",
                "commit": {
                    "message": "legacy parser fix",
                    "author": {"name": "dev", "date": "2023-12-30T00:00:00Z"},
                    "committer": {"date": "2023-12-30T00:00:00Z"},
                },
                "files": [
                    {"filename": "src/parser.py", "status": "modified", "patch": "@@ -1 +1 @@\n-old\n+new"}
                ],
            }
        if path == "/repos/o/r/commits":
            page = int((params or {}).get("page", 1))
            if page == 1:
                return [
                    {
                        "sha": "abc",
                        "html_url": "https://example/commit/abc",
                        "commit": {
                            "message": "Fix parser",
                            "author": {"name": "dev", "date": "2024-01-03T00:00:00Z"},
                            "committer": {"date": "2024-01-03T00:00:00Z"},
                        },
                    },
                    {
                        "sha": "old",
                        "html_url": "https://example/commit/old",
                        "commit": {
                            "message": "legacy parser fix",
                            "author": {"name": "dev", "date": "2023-12-30T00:00:00Z"},
                            "committer": {"date": "2023-12-30T00:00:00Z"},
                        },
                    },
                ]
            return []
        raise AssertionError(f"Unexpected request: {path} params={params}")

    def _request_text(self, path, *, params=None, accept="application/vnd.github.v3.diff"):  # noqa: ANN001, ARG002
        if path == "/repos/o/r/pulls/5":
            return "diff --git a/a.txt b/a.txt\nindex 111..222 100644\n--- a/a.txt\n+++ b/a.txt\n"
        raise AssertionError(f"Unexpected text request: {path} params={params}")


class ScopedGitHubMCPServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _import_module()

    def _make_client(self):  # noqa: ANN202
        class _Client(self.module.GitHubScopedClient):
            def _request(inner_self, path, *, params=None, accept="application/vnd.github+json"):  # noqa: ANN001, ARG002
                return FakeGitHubScopedClient._request(inner_self, path, params=params)

            def _request_text(inner_self, path, *, params=None, accept="application/vnd.github.v3.diff"):  # noqa: ANN001, ARG002
                return FakeGitHubScopedClient._request_text(inner_self, path, params=params)

        return _Client(token="t", repository="o/r", base_commit="base")

    def test_search_issues_filters_repo_artifacts(self) -> None:
        client = self._make_client()
        results = client.search_issues("parser", limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], 9)
        self.assertNotIn("url", results[0])
        self.assertEqual(results[0]["body"], "Old parser issue")

    def test_get_issue_rejects_new_artifact(self) -> None:
        client = self._make_client()
        with self.assertRaises(ValueError):
            client.get_issue(11)

    def test_search_commits_filters_by_query_and_cutoff(self) -> None:
        client = self._make_client()
        results = client.search_commits("parser", limit=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "old")
        self.assertNotIn("url", results[0])

    def test_get_pull_request_returns_textual_content(self) -> None:
        client = self._make_client()
        pr = client.get_pull_request(5, include_diff=True)
        self.assertEqual(pr["id"], 5)
        self.assertEqual(pr["body"], "Initial PR description")
        self.assertEqual(len(pr["issue_comments"]), 1)
        self.assertEqual(len(pr["review_comments"]), 1)
        self.assertEqual(len(pr["commits"]), 1)
        self.assertIn("diff --git", pr["diff"])

    def test_get_commit_returns_files_with_patch(self) -> None:
        client = self._make_client()
        commit = client.get_commit("old", include_diff=True)
        self.assertEqual(commit["id"], "old")
        self.assertEqual(commit["files"][0]["path"], "src/parser.py")
        self.assertIn("+new", commit["files"][0]["patch"])


if __name__ == "__main__":
    unittest.main()
