from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from mcp.server.fastmcp import FastMCP


def _parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitHubScopedClient:
    def __init__(self, token: str, repository: str, base_commit: str) -> None:
        if "/" not in repository:
            msg = "GITHUB_REPOSITORY must be in the form 'owner/repo'."
            raise ValueError(msg)
        self._token = token
        self._repository = repository
        self._owner, self._repo = repository.split("/", 1)
        self._base_commit = base_commit
        self._cutoff_datetime = self._load_cutoff_datetime()

    @property
    def cutoff_datetime_iso(self) -> str:
        return self._cutoff_datetime.isoformat()

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        encoded_params = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"https://api.github.com{path}"
        if encoded_params:
            url = f"{url}?{encoded_params}"

        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "scoped-github-mcp-server",
        }
        request = urllib.request.Request(url=url, headers=headers, method="GET")
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                details = error.read().decode("utf-8", errors="replace")
                msg = f"GitHub API request failed ({error.code}) for {path}: {details}"
                raise RuntimeError(msg) from error
            except http.client.IncompleteRead as exc:
                last_exc = exc
        msg = f"GitHub API response truncated after 3 attempts for {path}"
        raise RuntimeError(msg) from last_exc

    def _request_text(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github.v3.diff",
    ) -> str:
        encoded_params = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"https://api.github.com{path}"
        if encoded_params:
            url = f"{url}?{encoded_params}"
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "scoped-github-mcp-server",
        }
        request = urllib.request.Request(url=url, headers=headers, method="GET")
        last_exc: Exception | None = None
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                details = error.read().decode("utf-8", errors="replace")
                msg = f"GitHub API request failed ({error.code}) for {path}: {details}"
                raise RuntimeError(msg) from error
            except http.client.IncompleteRead as exc:
                last_exc = exc
        msg = f"GitHub API response truncated after 3 attempts for {path}"
        raise RuntimeError(msg) from last_exc

    def _load_cutoff_datetime(self) -> datetime:
        data = self._request(f"/repos/{self._owner}/{self._repo}/commits/{self._base_commit}")
        commit = data.get("commit", {})
        commit_date = commit.get("committer", {}).get("date") or commit.get("author", {}).get("date")
        if not commit_date:
            msg = f"Unable to resolve timestamp for commit '{self._base_commit}'."
            raise RuntimeError(msg)
        return _parse_github_datetime(commit_date)

    def _created_before_cutoff(self, created_at: str | None) -> bool:
        if not created_at:
            return False
        return _parse_github_datetime(created_at) < self._cutoff_datetime

    @staticmethod
    def _text_matches(query: str, *fields: str | None) -> bool:
        if not query:
            return True
        normalized = query.lower()
        return any(normalized in (field or "").lower() for field in fields)

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if limit < 1 or limit > 100:
            msg = "limit must be between 1 and 100."
            raise ValueError(msg)
        return limit

    @staticmethod
    def _issue_number_from_url(issue_url: str | None) -> int | None:
        if not issue_url:
            return None
        tail = issue_url.rstrip("/").split("/")[-1]
        if not tail.isdigit():
            return None
        return int(tail)

    def _collect_paginated(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        all_items: list[dict[str, Any]] = []
        page = 1
        while True:
            page_params = dict(params or {})
            page_params.update({"per_page": 100, "page": page})
            items = self._request(path, params=page_params)
            if not items:
                break
            all_items.extend(items)
            page += 1
        return all_items

    def _search_items(self, query: str, item_type: str, limit: int) -> list[dict[str, Any]]:
        cutoff_str = self._cutoff_datetime.isoformat().replace("+00:00", "Z")
        parts = [f"repo:{self._owner}/{self._repo}", f"created:<{cutoff_str}", f"type:{item_type}"]
        if query.strip():
            parts.insert(0, query.strip())
        q = " ".join(parts)
        matches: list[dict[str, Any]] = []
        page = 1
        while len(matches) < limit:
            data = self._request(
                "/search/issues",
                params={"q": q, "sort": "created", "order": "desc", "per_page": 100, "page": page},
            )
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                matches.append(item)
                if len(matches) >= limit:
                    break
            page += 1
        return matches

    def search_issues(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        return [
            {
                "id": item["number"],
                "title": item.get("title"),
                "body": item.get("body"),
                "state": item.get("state"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in self._search_items(query, "issue", limit)
        ]

    def search_pull_requests(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        return [
            {
                "id": item["number"],
                "title": item.get("title"),
                "body": item.get("body"),
                "state": item.get("state"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in self._search_items(query, "pr", limit)
        ]

    def search_comments(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        matches: list[dict[str, Any]] = []
        page = 1
        all_past_cutoff = False
        while len(matches) < limit:
            items = self._request(
                f"/repos/{self._owner}/{self._repo}/issues/comments",
                params={"sort": "created", "direction": "desc", "per_page": 100, "page": page},
            )
            if not items:
                break
            # items are sorted newest-first; once the first item is pre-cutoff, all remaining are too
            if not all_past_cutoff and self._created_before_cutoff(items[0].get("created_at")):
                all_past_cutoff = True
            for item in items:
                if not all_past_cutoff and not self._created_before_cutoff(item.get("created_at")):
                    continue
                if not self._text_matches(query, item.get("body")):
                    continue
                matches.append(
                    {
                        "id": item["id"],
                        "issue_id": self._issue_number_from_url(item.get("issue_url")),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                        "body": item.get("body"),
                    }
                )
                if len(matches) >= limit:
                    break
            page += 1
        return matches

    def search_commits(self, query: str, limit: int) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        matches: list[dict[str, Any]] = []
        page = 1
        while len(matches) < limit:
            items = self._request(
                f"/repos/{self._owner}/{self._repo}/commits",
                params={"per_page": 100, "page": page, "until": self.cutoff_datetime_iso},
            )
            if not items:
                break
            for item in items:
                commit_data = item.get("commit", {})
                created_at = (
                    commit_data.get("committer", {}).get("date") or commit_data.get("author", {}).get("date")
                )
                # `until` uses <= semantics; keep strict < check for boundary commits
                if not self._created_before_cutoff(created_at):
                    continue
                message = commit_data.get("message")
                if not self._text_matches(query, message):
                    continue
                matches.append(
                    {
                        "id": item.get("sha"),
                        "message": message,
                        "author": commit_data.get("author", {}).get("name"),
                        "created_at": created_at,
                    }
                )
                if len(matches) >= limit:
                    break
            page += 1
        return matches

    def get_issue(self, issue_id: int) -> dict[str, Any]:
        item = self._request(f"/repos/{self._owner}/{self._repo}/issues/{issue_id}")
        if "pull_request" in item:
            msg = f"Artifact {issue_id} is a pull request, not an issue."
            raise ValueError(msg)
        if not self._created_before_cutoff(item.get("created_at")):
            msg = f"Issue {issue_id} was not created prior to the configured commit cutoff."
            raise ValueError(msg)
        comments = [
            {
                "id": comment.get("id"),
                "author": comment.get("user", {}).get("login"),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
                "body": comment.get("body"),
            }
            for comment in self._collect_paginated(f"/repos/{self._owner}/{self._repo}/issues/{issue_id}/comments")
            if self._created_before_cutoff(comment.get("created_at"))
        ]
        return {
            "id": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "author": item.get("user", {}).get("login"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "body": item.get("body"),
            "labels": [label.get("name") for label in item.get("labels", [])],
            "comments": comments,
        }

    def get_pull_request(self, pr_id: int, include_diff: bool = False) -> dict[str, Any]:
        item = self._request(f"/repos/{self._owner}/{self._repo}/pulls/{pr_id}")
        if not self._created_before_cutoff(item.get("created_at")):
            msg = f"Pull request {pr_id} was not created prior to the configured commit cutoff."
            raise ValueError(msg)
        issue_comments = [
            {
                "id": comment.get("id"),
                "author": comment.get("user", {}).get("login"),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
                "body": comment.get("body"),
            }
            for comment in self._collect_paginated(f"/repos/{self._owner}/{self._repo}/issues/{pr_id}/comments")
            if self._created_before_cutoff(comment.get("created_at"))
        ]
        review_comments = [
            {
                "id": comment.get("id"),
                "author": comment.get("user", {}).get("login"),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
                "body": comment.get("body"),
            }
            for comment in self._collect_paginated(f"/repos/{self._owner}/{self._repo}/pulls/{pr_id}/comments")
            if self._created_before_cutoff(comment.get("created_at"))
        ]
        commits = []
        for commit in self._collect_paginated(f"/repos/{self._owner}/{self._repo}/pulls/{pr_id}/commits"):
            commit_data = commit.get("commit", {})
            created_at = commit_data.get("committer", {}).get("date") or commit_data.get("author", {}).get("date")
            if not self._created_before_cutoff(created_at):
                continue
            commits.append(
                {
                    "sha": commit.get("sha"),
                    "message": commit_data.get("message"),
                    "author": commit_data.get("author", {}).get("name"),
                    "created_at": created_at,
                }
            )
        diff = self._request_text(f"/repos/{self._owner}/{self._repo}/pulls/{pr_id}") if include_diff else None
        return {
            "id": item.get("number"),
            "title": item.get("title"),
            "state": item.get("state"),
            "author": item.get("user", {}).get("login"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "body": item.get("body"),
            "issue_comments": issue_comments,
            "review_comments": review_comments,
            "commits": commits,
            "diff": diff,
        }

    def get_comment(self, comment_id: int) -> dict[str, Any]:
        item = self._request(f"/repos/{self._owner}/{self._repo}/issues/comments/{comment_id}")
        if not self._created_before_cutoff(item.get("created_at")):
            msg = f"Comment {comment_id} was not created prior to the configured commit cutoff."
            raise ValueError(msg)
        return {
            "id": item.get("id"),
            "issue_id": self._issue_number_from_url(item.get("issue_url")),
            "author": item.get("user", {}).get("login"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "body": item.get("body"),
        }

    def get_commit(self, commit_sha: str, include_diff: bool = True) -> dict[str, Any]:
        item = self._request(f"/repos/{self._owner}/{self._repo}/commits/{commit_sha}")
        commit_data = item.get("commit", {})
        created_at = commit_data.get("committer", {}).get("date") or commit_data.get("author", {}).get("date")
        if not self._created_before_cutoff(created_at):
            msg = f"Commit {commit_sha} was not created prior to the configured commit cutoff."
            raise ValueError(msg)
        files = []
        for file_item in item.get("files", []):
            files.append(
                {
                    "path": file_item.get("filename"),
                    "status": file_item.get("status"),
                    "patch": file_item.get("patch") if include_diff else None,
                }
            )
        return {
            "id": item.get("sha"),
            "author": commit_data.get("author", {}).get("name"),
            "created_at": created_at,
            "message": commit_data.get("message"),
            "files": files,
        }

def _load_client_from_env() -> GitHubScopedClient:
    token = os.getenv("GITHUB_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY")
    base_commit = os.getenv("GITHUB_BASE_COMMIT")
    missing = [name for name, value in {"GITHUB_TOKEN": token, "GITHUB_REPOSITORY": repository, "GITHUB_BASE_COMMIT": base_commit}.items() if not value]
    if missing:
        msg = f"Missing required environment variables: {', '.join(missing)}"
        raise RuntimeError(msg)
    return GitHubScopedClient(token=token, repository=repository, base_commit=base_commit)


mcp = FastMCP("scoped-github-mcp")
_client: GitHubScopedClient | None = None


def _get_client() -> GitHubScopedClient:
    global _client
    if _client is None:
        _client = _load_client_from_env()
    return _client


@mcp.tool()
def search_issues(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search issues and return text content in the configured repository created before the base commit."""
    return _get_client().search_issues(query=query, limit=limit)


@mcp.tool()
def search_comments(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search issue comments and return text content created before the configured base commit."""
    return _get_client().search_comments(query=query, limit=limit)


@mcp.tool()
def search_pull_requests(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search pull requests and return text content created before the configured base commit."""
    return _get_client().search_pull_requests(query=query, limit=limit)


@mcp.tool()
def search_commits(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search commits and return commit messages created before the configured base commit."""
    return _get_client().search_commits(query=query, limit=limit)


@mcp.tool()
def get_issue(issue_id: int) -> dict[str, Any]:
    """Get issue text content (body and comments) by issue number, if created before the base commit."""
    return _get_client().get_issue(issue_id=issue_id)


@mcp.tool()
def get_comment(comment_id: int) -> dict[str, Any]:
    """Get issue comment text content by comment ID, if created before the configured base commit."""
    return _get_client().get_comment(comment_id=comment_id)


@mcp.tool()
def get_pull_request(pr_id: int, include_diff: bool = False) -> dict[str, Any]:
    """Get PR text content (description, comments, commits, optional diff) created before base commit."""
    return _get_client().get_pull_request(pr_id=pr_id, include_diff=include_diff)


@mcp.tool()
def get_commit(commit_sha: str, include_diff: bool = True) -> dict[str, Any]:
    """Get commit text content (message and file patches) created before the configured base commit."""
    return _get_client().get_commit(commit_sha=commit_sha, include_diff=include_diff)


if __name__ == "__main__":
    mcp.run()
