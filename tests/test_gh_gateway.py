"""Tests for the issue-tracker gateway's filtering.

Nothing is mocked: the gateway's own on-disk response cache is pre-seeded and it
runs in --replay mode, so these exercise the real request path (cache lookup,
pagination, rendering) with no network and no token.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from swebp_slurm.gh_gateway import (
    API,
    EDITS_QUERY,
    Gateway,
    GitHub,
    Instance,
    Refused,
    comment_visible,
    fix_sha_from_instance_id,
    forbidden_tokens,
    parse_ts,
    patch_patterns,
    pr_from_message,
    rewrite_query,
    state_at,
)

REPO = "acme/widget"
CUTOFF = "2023-06-05T09:18:55Z"
FIX_SHA = "04998908ba6721d64eba79ae3b65a351dcfbc5b5"
FIX_PR = 77
BEFORE, AFTER = "2023-05-01T10:00:00Z", "2023-07-01T10:00:00Z"


def comment(login: str, created: str, updated: str | None = None, body: str = "hello", **extra) -> dict:
    return {"user": {"login": login}, "created_at": created, "updated_at": updated or created, "body": body, **extra}


@pytest.fixture
def gateway(tmp_path: Path) -> Gateway:
    gw = Gateway(
        gh=GitHub(token="", cache_dir=tmp_path / "cache", replay=True),
        log_dir=tmp_path / "log",
        diffs=True,
    )
    gw.instances["inst"] = Instance(
        instance_id="inst",
        repo=REPO,
        base_commit="b" * 40,
        fix_sha=FIX_SHA,
        cutoff=parse_ts(CUTOFF),
        fix_pr=FIX_PR,
        fix_pr_source="api",
    )
    return gw


def seed(gw: Gateway, path: str, payload, accept: str = "application/vnd.github+json") -> None:
    target = gw.gh._cache_path(f"{accept} {API}{path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"url": API + path, "accept": accept, "text": json.dumps(payload)}))


def seed_edits(gw: Gateway, number: int, edits: list[tuple[str, str | None]]) -> None:
    body = json.dumps(
        {"query": EDITS_QUERY, "variables": {"owner": "acme", "name": "widget", "number": number}}
    ).encode()
    target = gw.gh._cache_path(f"graphql {hashlib.sha256(body).hexdigest()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "data": {
                    "data": {
                        "repository": {
                            "issueOrPullRequest": {
                                "userContentEdits": {"nodes": [{"editedAt": at, "diff": d} for at, d in edits]}
                            }
                        }
                    }
                }
            }
        )
    )


def seed_thread(
    gw: Gateway, number: int, issue: dict, comments: list[dict], edits: list[tuple[str, str | None]] | None = None
) -> None:
    seed(gw, f"/repos/{REPO}/issues/{number}", issue)
    seed(gw, f"/repos/{REPO}/issues/{number}/comments?per_page=100&page=1", comments)
    seed_edits(gw, number, edits or [])


ISSUE = {
    "number": 5,
    "title": "Email validation status is wrong in the ACP",
    "user": {"login": "reporter"},
    "created_at": "2019-01-02T00:00:00Z",
    "updated_at": AFTER,
    "closed_at": None,
    "body": "the admin panel shows Validated for pending users",
}


def test_thread_filters_comments_individually(gateway):
    """The failure this guards: filtering by thread instead of by artefact. The
    thread is from 2019, so a thread-level test would serve every comment."""
    seed_thread(
        gateway,
        5,
        ISSUE,
        [
            comment("alice", BEFORE, body="reproduced on 3.1"),
            comment("bob", AFTER, body="fixed in the latest release"),
            comment("carol", BEFORE, updated=AFTER, body="edited long after the cutoff"),
            comment("dave", "2023-01-01T00:00:00Z", updated=BEFORE, body="edited, but before the cutoff"),
        ],
    )
    text, stats = gateway.thread(gateway.instances["inst"], 5)

    assert "reproduced on 3.1" in text
    assert "edited, but before the cutoff" in text  # pre-cutoff edit: body is faithful
    assert "fixed in the latest release" not in text
    assert "edited long after the cutoff" not in text
    assert (stats["comments"], stats["dropped"]) == (2, 2)


def test_thread_refuses_the_fix_pr(gateway):
    with pytest.raises(Refused) as excinfo:
        gateway.thread(gateway.instances["inst"], FIX_PR)
    assert excinfo.value.status == 404


def test_thread_refuses_post_cutoff_issue(gateway):
    seed_thread(gateway, 9, {**ISSUE, "number": 9, "created_at": AFTER}, [])
    with pytest.raises(Refused):
        gateway.thread(gateway.instances["inst"], 9)


def test_comment_naming_the_fix_pr_is_dropped(gateway):
    """A pre-cutoff comment can legitimately say "opened #77" -- and #77 is the
    answer, so the mention has to go with it."""
    seed_thread(
        gateway,
        5,
        ISSUE,
        [comment("alice", BEFORE, body=f"I opened #{FIX_PR} for this"), comment("bob", BEFORE, body="thanks")],
    )
    text, stats = gateway.thread(gateway.instances["inst"], 5)
    assert f"#{FIX_PR}" not in text
    assert (stats["comments"], stats["dropped"]) == (1, 1)


def test_comment_naming_the_fix_sha_is_dropped(gateway):
    seed_thread(gateway, 5, ISSUE, [comment("alice", BEFORE, body=f"see {FIX_SHA[:7]} on master")])
    assert gateway.thread(gateway.instances["inst"], 5)[1]["comments"] == 0


def test_body_is_restored_to_its_text_at_the_cutoff(gateway):
    """`updated_at` is useless for bodies -- it bumps on any activity -- so the
    real edit history decides. Each edit node carries the body *after* that
    edit, so the newest pre-cutoff edit is the text that stood at the cutoff."""
    seed_thread(gateway, 5, ISSUE, [], edits=[(BEFORE, "the text as of the cutoff"), (AFTER, ISSUE["body"])])
    text, stats = gateway.thread(gateway.instances["inst"], 5)
    assert "the text as of the cutoff" in text and ISSUE["body"] not in text
    assert stats["body"] == "reconstructed"


def test_body_omitted_when_every_revision_postdates_the_cutoff(gateway):
    """Its original text is not in the history, so it is dropped, not guessed."""
    seed_thread(gateway, 5, ISSUE, [], edits=[(AFTER, ISSUE["body"])])
    text, stats = gateway.thread(gateway.instances["inst"], 5)
    assert ISSUE["body"] not in text and stats["body"] == "omitted:edited-after-cutoff"


def test_unedited_body_is_served_as_is(gateway):
    seed_thread(gateway, 6, {**ISSUE, "number": 6}, [], edits=[])
    text, stats = gateway.thread(gateway.instances["inst"], 6)
    assert ISSUE["body"] in text and stats["body"] == "served"

    seed_thread(gateway, 7, {**ISSUE, "number": 7}, [], edits=[(BEFORE, ISSUE["body"])])
    assert gateway.thread(gateway.instances["inst"], 7)[1]["body"] == "served"


def test_search_scopes_and_filters(gateway):
    query = rewrite_query("validation is:issue", REPO, parse_ts(CUTOFF))
    seed(
        gateway,
        f"/search/issues?q={urllib.parse.quote(query)}&per_page=30&sort=created&order=desc",
        {
            "items": [
                {"number": 5, "title": "validation is wrong", "created_at": BEFORE, "closed_at": None, "comments": 2},
                {"number": FIX_PR, "title": "fix validation", "created_at": BEFORE, "closed_at": None,
                 "pull_request": {}, "comments": 0},
                {"number": 9, "title": "later report", "created_at": AFTER, "closed_at": None, "comments": 0},
                {"number": 11, "title": f"tracked in #{FIX_PR}", "created_at": BEFORE, "closed_at": None, "comments": 0},
            ]
        },
    )
    text, stats = gateway.search(gateway.instances["inst"], "validation is:issue")
    assert "#5" in text
    assert (stats["results"], stats["dropped"]) == (1, 3)


@pytest.mark.parametrize(("limit", "per_page"), [(5, 5), (0, 1), (999, 50), (30, 30)])
def test_search_limit_is_clamped(gateway, limit, per_page):
    """The client forwards whatever number the agent typed after --limit."""
    query = rewrite_query("x", REPO, parse_ts(CUTOFF))
    seed(
        gateway,
        f"/search/issues?q={urllib.parse.quote(query)}&per_page={per_page}&sort=created&order=desc",
        {"items": []},
    )
    assert gateway.search(gateway.instances["inst"], "x", limit)[1]["results"] == 0


@pytest.mark.parametrize(
    ("query", "must_not_contain"),
    [
        ("repo:other/repo bug", "other/repo"),
        ("bug created:>2024-01-01", "2024-01-01"),
        ("org:evil user:evil bug", "evil"),
    ],
)
def test_rewrite_query_strips_scope_qualifiers(query, must_not_contain):
    """No query may widen its own reach."""
    rewritten = rewrite_query(query, REPO, parse_ts(CUTOFF))
    assert must_not_contain not in rewritten
    assert f"repo:{REPO}" in rewritten and "created:<2023-06-05T09:18:55Z" in rewritten


def test_state_is_reported_as_of_the_cutoff():
    """Every instance's issue is closed today by construction, so the current
    state would be a giveaway."""
    assert state_at({"closed_at": AFTER}, parse_ts(CUTOFF)) == "open"
    assert state_at({"closed_at": BEFORE}, parse_ts(CUTOFF)) == "closed"
    assert state_at({"closed_at": None}, parse_ts(CUTOFF)) == "open"


@pytest.mark.parametrize(
    ("created", "updated", "expected"),
    [
        (BEFORE, BEFORE, True),
        ("2023-01-01T00:00:00Z", BEFORE, True),  # edited, but before the cutoff
        (BEFORE, AFTER, False),
        (AFTER, AFTER, False),
    ],
)
def test_comment_visibility_turns_on_updated_at(created, updated, expected):
    assert comment_visible(comment("x", created, updated), parse_ts(CUTOFF), [])[0] is expected


def test_budget_is_enforced_and_explicit(gateway):
    gateway.budget_search = 2
    instance = gateway.instances["inst"]
    seed(gateway, f"/search/issues?q={urllib.parse.quote(rewrite_query('x', REPO, parse_ts(CUTOFF)))}"
         "&per_page=30&sort=created&order=desc", {"items": []})
    gateway.search(instance, "x")
    gateway.search(instance, "x")
    with pytest.raises(Refused) as excinfo:
        gateway.search(instance, "x")
    assert excinfo.value.status == 429 and "budget exhausted" in str(excinfo.value)


def test_diff_of_open_pr_stops_at_the_cutoff(gateway):
    """The current diff would include commits pushed after the cutoff."""
    seed(gateway, f"/repos/{REPO}/pulls/12", {
        "number": 12, "created_at": BEFORE, "merged_at": AFTER,
        "merge_commit_sha": "c" * 40, "base": {"sha": "a" * 40},
    })
    seed(gateway, f"/repos/{REPO}/pulls/12/commits?per_page=100&page=1", [
        {"sha": "d" * 40, "commit": {"committer": {"date": BEFORE}}},
        {"sha": "e" * 40, "commit": {"committer": {"date": AFTER}}},
    ])
    seed(gateway, f"/repos/{REPO}/compare/{'a' * 40}...{'d' * 40}", "diff --git a/x b/x\n",
         accept="application/vnd.github.v3.diff")
    text, stats = gateway.diff(gateway.instances["inst"], 12)
    assert stats["commits_before_cutoff"] == 1 and "e" * 40 not in text


def test_diff_of_pr_merged_before_cutoff_points_at_local_git(gateway):
    """It is already an ancestor of the checked-out commit, so no API call and
    nothing the agent could not already reach."""
    seed(gateway, f"/repos/{REPO}/pulls/13", {
        "number": 13, "created_at": BEFORE, "merged_at": BEFORE,
        "merge_commit_sha": "f" * 40, "base": {"sha": "a" * 40},
    })
    text, stats = gateway.diff(gateway.instances["inst"], 13)
    assert stats["kind"] == "merged-locally-available" and f"git show {'f' * 40}" in text


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ('Fixes for "validate email" in ACP (#11677)\n\n* changes', 11677),
        ("Merge pull request #42 from foo/bar", None),
        ("chore: bump deps (#1)", 1),
        ("mentions (#99) mid-subject and ends here", None),
    ],
)
def test_pr_from_message(message, expected):
    assert pr_from_message(message) == expected


@pytest.mark.parametrize(
    ("instance_id", "expected"),
    [
        ("instance_NodeBB__NodeBB-04998908ba6721d64eba79ae3b65a351dcfbc5b5-vnan", "04998908ba6721d64eba79ae3b65a351dcfbc5b5"),
        ("instance_flipt-abc1234-vf2cf3cbd", "abc1234"),
    ],
)
def test_fix_sha_from_instance_id(instance_id, expected):
    assert fix_sha_from_instance_id(instance_id) == expected


def test_forbidden_tokens_cover_short_shas_and_pr_references():
    tokens = forbidden_tokens(FIX_SHA, FIX_PR)
    assert FIX_SHA[:7] in tokens and f"#{FIX_PR}" in tokens and f"/pull/{FIX_PR}" in tokens


def test_patch_patterns_keeps_only_distinctive_added_lines():
    patch = (
        "--- a/x\n+++ b/x\n"
        "+short\n"
        "+++ ignored header line that is quite long indeed\n"
        "-removed line that is long enough to be distinctive\n"
        "+    if user.validation_state == VALIDATION_PENDING and not expired:\n"
        "+    if user.validation_state == VALIDATION_PENDING and not expired:\n"
    )
    assert patch_patterns(patch) == ["if user.validation_state == VALIDATION_PENDING and not expired:"]
