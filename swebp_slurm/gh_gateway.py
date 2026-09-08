"""Read-only GitHub gateway for the issue-tracker experiment.

Serves one SWE-Bench Pro instance's issue tracker *as it looked at its base
commit*: the instance's own repository only, nothing created after the cutoff,
and never the pull request that produced the fix.

The filters live here, on the host, because the agent is root inside its own
container -- no in-container permission scheme would hold. The container gets a
tiny `gh` client (see gh_client.py) and never sees the GitHub token.

Run one gateway per benchmark run (slurm/gh_gateway.sbatch). It writes an
endpoint descriptor that gen_array discovers the way it already discovers the
model endpoint, and it is the single place API rate limits are accounted for:
the Search API allows only 30 requests/minute per token, which a 10-wide job
array would otherwise blow through immediately.

Every upstream response is cached by URL and every agent-facing request is
logged, so a rerun replays from cache (--replay never calls GitHub at all) and
the log is the evidence the `audit` subcommand checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import typer

API = "https://api.github.com"
# Issue and PR *bodies* are checked for post-cutoff edits through GraphQL; REST
# exposes no edit history at all, only `updated_at`, which on an issue bumps on
# any activity and so says nothing about the body.
EDITS_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    issueOrPullRequest(number:$number){
      ... on Issue       { userContentEdits(last:100){nodes{editedAt}} }
      ... on PullRequest { userContentEdits(last:100){nodes{editedAt}} }
    }
  }
}
"""
MAX_DIFF_BYTES = 256 * 1024

app = typer.Typer(rich_markup_mode="rich", add_completion=False)


class Refused(Exception):
    """A request the gateway will not serve. `status` becomes the HTTP status."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# Pure helpers -- no network, no state. Everything the filtering rests on lives
# here so it can be tested without a token (tests/test_gh_gateway.py).
# --------------------------------------------------------------------------- #


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 stamp from either GitHub (`...Z`) or git (`...+02:00`)."""
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr_from_message(message: str) -> int | None:
    """The `(#N)` a squash merge appends to the subject line."""
    m = re.search(r"\(#(\d+)\)\s*$", message.splitlines()[0])
    return int(m.group(1)) if m else None


def fix_sha_from_instance_id(instance_id: str) -> str:
    """`instance_<Owner>__<Repo>-<fix_sha>-v<env_sha|nan>`.

    The dataset carries no PR or issue number, so this sha is the only thread
    back to the pull request that has to be excluded.
    """
    m = re.search(r"-([0-9a-f]{7,40})-v", instance_id)
    if not m:
        raise ValueError(f"no fix sha in instance_id {instance_id!r}")
    return m.group(1)


def forbidden_tokens(fix_sha: str, fix_pr: int | None) -> list[str]:
    """Strings that would name the fix.

    A pre-cutoff comment can legitimately say "opened #11677" about a PR that is
    still open at the cutoff -- and that PR is the answer, so the mention goes
    with it.
    """
    tokens = [fix_sha, fix_sha[:10], fix_sha[:7]]
    if fix_pr:
        tokens += [f"#{fix_pr}", f"GH-{fix_pr}", f"/pull/{fix_pr}", f"/issues/{fix_pr}"]
    return [t for t in tokens if t]


def find_forbidden(text: str, tokens: list[str]) -> str | None:
    return next((t for t in tokens if t in (text or "")), None)


def comment_visible(comment: dict, cutoff: datetime, tokens: list[str]) -> tuple[bool, str]:
    """Serve a comment iff `updated_at <= cutoff`.

    That single test is the whole guarantee, and it is an *inclusion* rule: if a
    comment's last edit predates the cutoff, the body the API returns today is
    byte-for-byte the body that existed at the cutoff, so an edited-but-
    pre-cutoff comment is exactly faithful rather than merely tolerated. Since
    `updated_at >= created_at` always, it also implies `created_at <= cutoff`.
    """
    if parse_ts(comment["updated_at"]) > cutoff:
        return False, "edited-or-created-after-cutoff"
    if token := find_forbidden(comment.get("body") or "", tokens):
        return False, f"names-the-fix:{token}"
    return True, ""


def state_at(issue: dict, cutoff: datetime) -> str:
    """The state as of the cutoff -- never the current one, which is `closed`
    for every instance in the dataset by construction."""
    closed = issue.get("closed_at")
    return "closed" if closed and parse_ts(closed) <= cutoff else "open"


def body_edited_after(edit_nodes: list[dict], cutoff: datetime) -> bool:
    return any(parse_ts(n["editedAt"]) > cutoff for n in edit_nodes if n.get("editedAt"))


SCOPE_QUALIFIER = re.compile(r"\b(?:repo|org|user|created|updated|closed|merged):\S+", re.I)


def rewrite_query(query: str, repo: str, cutoff: datetime) -> str:
    """Force the scope. Any repo/time qualifier the agent supplied is stripped
    first, so no query can widen its own reach."""
    stripped = SCOPE_QUALIFIER.sub("", query).strip()
    return f"{stripped} repo:{repo} created:<{fmt_ts(cutoff)}".strip()


def patch_patterns(patch: str, *, min_len: int = 40, cap: int = 200) -> list[str]:
    """Distinctive added lines of the gold patch, for the leakage audit."""
    seen: dict[str, None] = {}
    for line in patch.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        candidate = line[1:].strip()
        if len(candidate) >= min_len and len(re.findall(r"\w", candidate)) >= 3:
            seen.setdefault(candidate, None)
    return list(seen)[:cap]


# --------------------------------------------------------------------------- #
# Upstream access: rate limiting, caching, retries.
# --------------------------------------------------------------------------- #


@dataclass
class Bucket:
    """Token bucket. GitHub's own headers are authoritative and are honoured on
    top of this; the bucket is what stops a 10-wide array racing into the
    secondary limits in the first place."""

    capacity: int
    per_seconds: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(
                    float(self.capacity),
                    self.tokens + (now - self.updated) * self.capacity / self.per_seconds,
                )
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) * self.per_seconds / self.capacity
            time.sleep(min(wait, 5.0))


@dataclass
class GitHub:
    token: str
    cache_dir: Path
    replay: bool = False
    calls: int = 0
    cache_hits: int = 0
    core: Bucket = field(default_factory=lambda: Bucket(5000, 3600))
    search: Bucket = field(default_factory=lambda: Bucket(30, 60))

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest}.json"

    def _store(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)

    def fetch(self, url: str, *, accept: str = "application/vnd.github+json", family: str = "core") -> str:
        path = self._cache_path(f"{accept} {url}")
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())["text"]
        if self.replay:
            raise Refused(f"replay mode: {url} is not in the cache", 503)

        (self.search if family == "search" else self.core).take()
        for attempt in range(4):
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": accept,
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "swebp-gh-gateway",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    self.calls += 1
                    text = response.read().decode("utf-8", "replace")
                self._store(path, {"url": url, "accept": accept, "text": text})
                return text
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise Refused("not found", 404) from e
                if e.code in (403, 429) and attempt < 3:
                    time.sleep(self._retry_after(e.headers, attempt))
                    continue
                raise Refused(f"upstream {e.code}: {e.reason}", 502) from e
            except (urllib.error.URLError, socket.timeout) as e:
                if attempt == 3:
                    raise Refused(f"upstream unreachable: {e}", 502) from e
                time.sleep(2**attempt)
        raise Refused("upstream retries exhausted", 502)

    @staticmethod
    def _retry_after(headers, attempt: int) -> float:
        value = headers.get("Retry-After")
        if value and value.isdigit():
            return min(float(value), 300.0)
        if headers.get("X-RateLimit-Remaining") == "0" and (reset := headers.get("X-RateLimit-Reset")):
            return max(0.0, min(float(reset) - time.time() + 5, 900.0))
        return float(2**attempt)

    def json(self, path: str, *, family: str = "core"):
        return json.loads(self.fetch(f"{API}{path}", family=family))

    def paged(self, path: str, *, limit: int = 300) -> list[dict]:
        """`per_page=100`, walking `page=` until short or `limit`. Link-header
        parsing buys nothing here: every list endpoint we use accepts `page`,
        and a short page always means the end."""
        joiner = "&" if "?" in path else "?"
        items: list[dict] = []
        for page in range(1, limit // 100 + 2):
            batch = self.json(f"{path}{joiner}per_page=100&page={page}")
            items += batch
            if len(batch) < 100 or len(items) >= limit:
                break
        return items[:limit]

    def graphql(self, query: str, variables: dict) -> dict:
        body = json.dumps({"query": query, "variables": variables}).encode()
        path = self._cache_path(f"graphql {hashlib.sha256(body).hexdigest()}")
        if path.exists():
            self.cache_hits += 1
            return json.loads(path.read_text())["data"]
        if self.replay:
            raise Refused("replay mode: graphql query is not in the cache", 503)
        self.core.take()
        request = urllib.request.Request(
            f"{API}/graphql",
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "User-Agent": "swebp-gh-gateway"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                self.calls += 1
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, socket.timeout) as e:
            raise Refused(f"graphql unreachable: {e}", 502) from e
        self._store(path, {"data": data})
        return data


# --------------------------------------------------------------------------- #
# Per-instance context and the served views.
# --------------------------------------------------------------------------- #


@dataclass
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    fix_sha: str
    cutoff: datetime
    fix_pr: int | None
    fix_pr_source: str
    searches: int = 0
    details: int = 0

    @property
    def tokens(self) -> list[str]:
        return forbidden_tokens(self.fix_sha, self.fix_pr)


@dataclass
class Gateway:
    gh: GitHub
    log_dir: Path
    diffs: bool = False
    budget_search: int = 40
    budget_detail: int = 200
    allow_unresolved: bool = False
    instances: dict[str, Instance] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # -- registration ------------------------------------------------------- #

    def register(self, payload: dict) -> dict:
        """Resolve the fix PR and pin the cutoff.

        The container's own git is the ground truth for the cutoff -- it is the
        tree the agent will actually edit -- so GitHub is asked only to confirm
        that this really is the upstream base commit. Three calls per instance,
        cached forever.
        """
        instance_id, repo = payload["instance_id"], payload["repo"]
        fix_sha, base_commit = payload["fix_sha"], payload["base_commit"]
        if known := self.instances.get(instance_id):
            return self._registration(known, "cached")

        commit = self.gh.json(f"/repos/{repo}/commits/{fix_sha}")
        parents = [p["sha"] for p in commit.get("parents", [])]
        if base_commit not in parents:
            raise Refused(
                f"{fix_sha[:10]} does not have {base_commit[:10]} as a parent (parents: "
                f"{[p[:10] for p in parents]}); the cutoff and the fix PR would both be wrong",
                409,
            )
        base = self.gh.json(f"/repos/{repo}/commits/{base_commit}")
        upstream_author = parse_ts(base["commit"]["author"]["date"])
        if upstream_author != parse_ts(payload["head_authored_at"]):
            raise Refused(
                f"container HEAD author date {payload['head_authored_at']} != upstream "
                f"{fmt_ts(upstream_author)} for {base_commit[:10]}; refusing to serve a "
                "snapshot cut at the wrong time",
                409,
            )

        fix_pr, source = self._resolve_pr(repo, fix_sha, commit)
        if fix_pr is None and not self.allow_unresolved:
            raise Refused(
                f"cannot identify the pull request for {fix_sha[:10]}; it would be served "
                "as ordinary pre-cutoff context. Drop this instance from both arms, or set "
                "SWEBP_GH_ALLOW_UNRESOLVED=1 to accept the risk",
                409,
            )
        instance = Instance(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            fix_sha=fix_sha,
            cutoff=parse_ts(payload["head_committed_at"]),
            fix_pr=fix_pr,
            fix_pr_source=source,
        )
        with self.lock:
            self.instances[instance_id] = instance
        warning = ""
        if parse_ts(base["commit"]["committer"]["date"]) != instance.cutoff:
            warning = (
                f"committer-date drift: upstream {base['commit']['committer']['date']} vs "
                f"container {payload['head_committed_at']}; using the container's"
            )
        return self._registration(instance, "resolved", warning)

    def _resolve_pr(self, repo: str, fix_sha: str, commit: dict) -> tuple[int | None, str]:
        pulls = self.gh.json(f"/repos/{repo}/commits/{fix_sha}/pulls")
        for pull in pulls:
            if pull.get("merge_commit_sha") == fix_sha:
                return pull["number"], "api-merge-commit"
        if pulls:
            return pulls[0]["number"], "api"
        if number := pr_from_message(commit["commit"]["message"]):
            return number, "squash-message"
        return None, "unresolved"

    @staticmethod
    def _registration(instance: Instance, status: str, warning: str = "") -> dict:
        return {
            "status": status,
            "instance_id": instance.instance_id,
            "repo": instance.repo,
            "cutoff": fmt_ts(instance.cutoff),
            "fix_pr": instance.fix_pr,
            "fix_pr_source": instance.fix_pr_source,
            "warning": warning,
        }

    def instance(self, instance_id: str) -> Instance:
        if instance := self.instances.get(instance_id):
            return instance
        raise Refused(f"instance {instance_id!r} is not registered with this gateway", 404)

    def _spend(self, instance: Instance, kind: str) -> None:
        with self.lock:
            if kind == "search":
                instance.searches += 1
                used, budget = instance.searches, self.budget_search
            else:
                instance.details += 1
                used, budget = instance.details, self.budget_detail
        if used > budget:
            raise Refused(
                f"{kind} budget exhausted for this task ({budget}). No further tracker "
                "requests will be served; continue with the code you have.",
                429,
            )

    # -- views -------------------------------------------------------------- #

    def search(self, instance: Instance, query: str) -> tuple[str, dict]:
        self._spend(instance, "search")
        q = rewrite_query(query, instance.repo, instance.cutoff)
        payload = self.gh.json(
            f"/search/issues?q={urllib.parse.quote(q)}&per_page=30&sort=created&order=desc",
            family="search",
        )
        dropped, lines, dates = 0, [], []
        for item in payload.get("items", []):
            created = parse_ts(item["created_at"])
            if item["number"] == instance.fix_pr or created >= instance.cutoff:
                dropped += 1
                continue
            if find_forbidden(item.get("title") or "", instance.tokens):
                dropped += 1
                continue
            dates.append(item["created_at"])
            kind = "pr" if item.get("pull_request") else "issue"
            lines.append(
                f"#{item['number']}\t{kind}\t{state_at(item, instance.cutoff)}\t"
                f"{item['created_at']}\t{item.get('comments', 0)} comments\t{item['title']}"
            )
        header = f"# {len(lines)} result(s) in {instance.repo}, created before {fmt_ts(instance.cutoff)}"
        body = "\n".join([header, *lines]) if lines else f"{header}\n(no matches)"
        return body + "\n", {"dropped": dropped, "results": len(lines), "dates": dates, "query": q}

    def thread(self, instance: Instance, number: int) -> tuple[str, dict]:
        self._spend(instance, "detail")
        if number == instance.fix_pr:
            raise Refused(f"#{number} is not available", 404)
        issue = self.gh.json(f"/repos/{instance.repo}/issues/{number}")
        if parse_ts(issue["created_at"]) >= instance.cutoff:
            raise Refused(f"#{number} was created after {fmt_ts(instance.cutoff)}", 404)

        stats = {"comments": 0, "dropped": 0, "body": "served", "dates": [issue["created_at"]]}
        body = self._body_at_cutoff(instance, number, issue, stats)
        is_pr = bool(issue.get("pull_request"))
        head = [
            f"# [{'pr' if is_pr else 'issue'} #{number}] {issue['title']}",
            f"opened by {(issue.get('user') or {}).get('login', '?')} on {issue['created_at']}"
            f" -- {state_at(issue, instance.cutoff)} as of {fmt_ts(instance.cutoff)}",
            "",
            body,
        ]
        if is_pr:
            head += self._merged_hint(instance, number, stats)

        comments = self.gh.paged(f"/repos/{instance.repo}/issues/{number}/comments")
        if is_pr:
            comments += self.gh.paged(f"/repos/{instance.repo}/pulls/{number}/comments")
        rendered = []
        for comment in sorted(comments, key=lambda c: c["created_at"]):
            ok, reason = comment_visible(comment, instance.cutoff, instance.tokens)
            if not ok:
                stats["dropped"] += 1
                continue
            stats["comments"] += 1
            stats["dates"].append(comment["created_at"])
            where = f" ({comment['path']})" if comment.get("path") else ""
            rendered.append(
                f"\n## comment by {(comment.get('user') or {}).get('login', '?')} on "
                f"{comment['created_at']}{where}\n\n{comment.get('body') or ''}"
            )
        return "\n".join(head + rendered) + "\n", stats

    def _body_at_cutoff(self, instance: Instance, number: int, issue: dict, stats: dict) -> str:
        """The body as of the cutoff, or nothing.

        `updated_at` is useless here -- on an issue it bumps on any activity --
        so the real edit history is read from GraphQL. If the body was edited
        after the cutoff we cannot reconstruct what it said and omit it; if the
        history cannot be read at all we do the same rather than guess.
        """
        owner, name = instance.repo.split("/", 1)
        try:
            data = self.gh.graphql(EDITS_QUERY, {"owner": owner, "name": name, "number": number})
            node = ((data.get("data") or {}).get("repository") or {}).get("issueOrPullRequest") or {}
            nodes = (node.get("userContentEdits") or {}).get("nodes") or []
        except Refused:
            stats["body"] = "omitted:edit-history-unavailable"
            return "(body omitted: its edit history could not be verified against the cutoff)"
        if body_edited_after(nodes, instance.cutoff):
            stats["body"] = "omitted:edited-after-cutoff"
            return "(body omitted: it was edited after the cutoff, so its text at the cutoff is unknown)"
        if token := find_forbidden(issue.get("body") or "", instance.tokens):
            stats["body"] = f"omitted:names-the-fix:{token}"
            return "(body omitted)"
        return issue.get("body") or "(empty)"

    def _merged_hint(self, instance: Instance, number: int, stats: dict) -> list[str]:
        pull = self.gh.json(f"/repos/{instance.repo}/pulls/{number}")
        merged_at, merge_sha = pull.get("merged_at"), pull.get("merge_commit_sha")
        if merged_at and parse_ts(merged_at) <= instance.cutoff and merge_sha:
            stats["merge_commit"] = merge_sha
            return ["", f"merged {merged_at} as {merge_sha} -- `git show {merge_sha}` for the diff"]
        return ["", f"not merged as of {fmt_ts(instance.cutoff)}"]

    def diff(self, instance: Instance, number: int) -> tuple[str, dict]:
        self._spend(instance, "detail")
        if not self.diffs:
            raise Refused("diffs are disabled for this run (--gh-diffs)", 403)
        if number == instance.fix_pr:
            raise Refused(f"#{number} is not available", 404)
        pull = self.gh.json(f"/repos/{instance.repo}/pulls/{number}")
        if parse_ts(pull["created_at"]) >= instance.cutoff:
            raise Refused(f"#{number} was created after {fmt_ts(instance.cutoff)}", 404)

        merged_at, merge_sha = pull.get("merged_at"), pull.get("merge_commit_sha")
        if merged_at and parse_ts(merged_at) <= instance.cutoff and merge_sha:
            return (
                f"#{number} was merged before the cutoff as {merge_sha}, which is in your "
                f"repository's history. Run `git show {merge_sha}`.\n",
                {"kind": "merged-locally-available", "dates": [merged_at]},
            )

        # Open at the cutoff: the current diff would include commits pushed
        # after it, so diff the branch as it stood at the cutoff instead.
        commits = self.gh.paged(f"/repos/{instance.repo}/pulls/{number}/commits")
        pre = [c for c in commits if parse_ts(c["commit"]["committer"]["date"]) < instance.cutoff]
        if not pre:
            raise Refused(f"#{number} had no commits before {fmt_ts(instance.cutoff)}", 404)
        head, base_sha = pre[-1]["sha"], pull["base"]["sha"]
        text = self.gh.fetch(
            f"{API}/repos/{instance.repo}/compare/{base_sha}...{head}",
            accept="application/vnd.github.v3.diff",
        )
        truncated = len(text.encode()) > MAX_DIFF_BYTES
        if truncated:
            text = text.encode()[:MAX_DIFF_BYTES].decode("utf-8", "ignore") + "\n(diff truncated)\n"
        header = f"# diff of #{number} as of {fmt_ts(instance.cutoff)} ({base_sha[:10]}...{head[:10]})\n"
        return header + text, {
            "kind": "open-at-cutoff",
            "commits_before_cutoff": len(pre),
            "truncated": truncated,
            "dates": [c["commit"]["committer"]["date"] for c in pre],
        }

    # -- logging ------------------------------------------------------------ #

    def log(self, instance_id: str, record: dict) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self.lock:
            with (self.log_dir / f"{instance_id}.jsonl").open("a") as f:
                f.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------------- #
# HTTP surface
# --------------------------------------------------------------------------- #


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # quiet; we keep our own log
            pass

        def _reply(self, status: int, text: str) -> None:
            payload = text.encode("utf-8", "replace")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            url = urllib.parse.urlparse(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == "/health":
                return self._reply(200, "ok\n")
            started = time.time()
            instance_id = query.get("instance", "")
            try:
                instance = gateway.instance(instance_id)
                if url.path == "/search":
                    text, stats = gateway.search(instance, query.get("q", ""))
                elif url.path == "/thread":
                    text, stats = gateway.thread(instance, int(query["n"]))
                elif url.path == "/diff":
                    text, stats = gateway.diff(instance, int(query["n"]))
                else:
                    raise Refused(f"no such endpoint: {url.path}", 404)
                status = 200
            except Refused as e:
                text, stats, status = f"{e}\n", {"refused": str(e)}, e.status
            except (KeyError, ValueError) as e:
                text, stats, status = f"bad request: {e}\n", {"refused": str(e)}, 400
            gateway.log(
                instance_id or "_unregistered",
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "endpoint": url.path,
                    "params": query,
                    "status": status,
                    "seconds": round(time.time() - started, 3),
                    "text": text,
                    **stats,
                },
            )
            self._reply(status, text)

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/register":
                return self._reply(404, "no such endpoint\n")
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
            try:
                result = gateway.register(payload)
                status = 200
            except Refused as e:
                result, status = {"status": "refused", "error": str(e)}, e.status
            except KeyError as e:
                result, status = {"status": "refused", "error": f"missing field {e}"}, 400
            gateway.log(payload.get("instance_id", "_unregistered"), {"endpoint": "/register", **result})
            self._reply(status, json.dumps(result) + "\n")

    return Handler


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


# fmt: off
@app.command()
def serve(
    run: str = typer.Option(..., "--run", help="Run name; namespaces the descriptor and the request log"),
    endpoint_dir: Path = typer.Option(None, "--endpoint-dir", help="Where to write gh-gateway-<run>.json (default $SWEBP_GH_ENDPOINT_DIR)"),
    log_dir: Path = typer.Option(None, "--log-dir", help="Request log directory (default runs/<run>/gh_log)"),
    cache_dir: Path = typer.Option(None, "--cache", help="Upstream response cache (default $SWEBP_GH_CACHE)"),
    port: int = typer.Option(0, "--port", help="0 picks a free port"),
    diffs: bool = typer.Option(False, "--diffs/--no-diffs", help="Serve diffs of PRs that were open at the cutoff"),
    budget_search: int = typer.Option(40, "--budget-search", help="Search requests per instance"),
    budget_detail: int = typer.Option(200, "--budget-detail", help="Thread/diff requests per instance"),
    replay: bool = typer.Option(False, "--replay", help="Serve only from cache; never call GitHub"),
    allow_unresolved: bool = typer.Option(False, "--allow-unresolved", help="Serve instances whose fix PR could not be identified (unsafe)"),
) -> None:
    # fmt: on
    """Serve the cutoff-filtered issue tracker for one benchmark run."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token and not replay:
        raise typer.BadParameter(
            "GITHUB_TOKEN is not set. Put a PAT (public_repo / read-only public) in "
            "~/.config/mini-swe-agent/.env; unauthenticated is 60 requests/hour and useless here."
        )
    gateway = Gateway(
        gh=GitHub(token=token, cache_dir=cache_dir or _env_path("SWEBP_GH_CACHE", "~/.cache/swebp-gh"), replay=replay),
        log_dir=log_dir or Path("runs") / run / "gh_log",
        diffs=diffs,
        budget_search=budget_search,
        budget_detail=budget_detail,
        allow_unresolved=allow_unresolved or os.environ.get("SWEBP_GH_ALLOW_UNRESOLVED") == "1",
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(gateway))
    host = socket.gethostname()
    descriptor = (endpoint_dir or _env_path("SWEBP_GH_ENDPOINT_DIR", "endpoint")) / f"gh-gateway-{run}.json"
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text(
        json.dumps(
            {
                "host": host,
                "port": server.server_address[1],
                "base_url": f"http://{host}:{server.server_address[1]}",
                "run": run,
                "job_id": os.environ.get("SLURM_JOB_ID", ""),
                "diffs": diffs,
                "replay": replay,
                "started": datetime.now(timezone.utc).isoformat(),
                "ready": True,
            },
            indent=2,
        )
    )
    print(f"gh-gateway for run {run!r} on http://{host}:{server.server_address[1]} -> {descriptor}", flush=True)
    # Slurm ends a job with SIGTERM; without this the descriptor outlives the
    # process and gen tasks would discover a `ready` gateway on a dead node.
    # shutdown() blocks until serve_forever returns, so it cannot be called from
    # the handler itself -- that thread is the one sitting in serve_forever.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    finally:
        descriptor.unlink(missing_ok=True)
        print(f"upstream calls: {gateway.gh.calls}, cache hits: {gateway.gh.cache_hits}", flush=True)


# fmt: off
@app.command()
def audit(
    run_dir: Path = typer.Argument(..., help="runs/<run> -- needs instances.jsonl and gh_log/"),
) -> None:
    # fmt: on
    """Check every payload the gateway served against the instance's gold patch.

    sha / PR / cutoff hits mean the filter regressed and are fatal. Gold-patch
    line hits are reported but not fatal: a pre-existing issue quoting the
    eventual fix is the phenomenon under study, and must be inspected rather
    than silently dropped.
    """
    instances = {
        record["instance_id"]: record
        for record in (json.loads(line) for line in (run_dir / "instances.jsonl").read_text().splitlines())
    }
    summary, fatal = {}, 0
    for log in sorted((run_dir / "gh_log").glob("*.jsonl")):
        instance = instances.get(log.stem)
        if instance is None:
            continue
        records = [json.loads(line) for line in log.read_text().splitlines()]
        registration = next((r for r in records if r.get("endpoint") == "/register"), {})
        cutoff = parse_ts(registration["cutoff"]) if registration.get("cutoff") else None
        fix_pr = registration.get("fix_pr")
        fix_sha = fix_sha_from_instance_id(instance["instance_id"])
        patterns = patch_patterns(instance.get("patch", ""))
        tokens = forbidden_tokens(fix_sha, fix_pr)

        served = [r for r in records if r.get("status") == 200 and r.get("text")]
        hits = [
            {"endpoint": r["endpoint"], "params": r.get("params", {}), "pattern": p}
            for r in served
            for p in patterns
            if p in r["text"]
        ]
        token_hits = [
            {"endpoint": r["endpoint"], "token": t} for r in served for t in tokens if t in r["text"]
        ]
        violations = [
            {"endpoint": r["endpoint"], "date": d}
            for r in served
            for d in r.get("dates", [])
            if cutoff and parse_ts(d) > cutoff
        ]
        entry = {
            "cutoff": registration.get("cutoff"),
            "fix_pr": fix_pr,
            "fix_pr_source": registration.get("fix_pr_source"),
            "requests": len(records),
            "searches": sum(r.get("endpoint") == "/search" for r in records),
            "threads": sum(r.get("endpoint") == "/thread" for r in records),
            "diffs": sum(r.get("endpoint") == "/diff" for r in records),
            "refusals": sum("refused" in r for r in records),
            "patch_line_hits": hits[:20],
            "token_hits": token_hits,
            "cutoff_violations": violations,
        }
        fatal += len(token_hits) + len(violations)
        summary[log.stem] = entry

    used = sum(1 for e in summary.values() if e["searches"] or e["threads"])
    out = {
        "instances_logged": len(summary),
        "instances_that_used_the_tracker": used,
        "fatal_findings": fatal,
        "patch_line_hits": sum(len(e["patch_line_hits"]) for e in summary.values()),
        "per_instance": summary,
    }
    (run_dir / "gh_log" / "_summary.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k != "per_instance"}, indent=2))
    if fatal:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
