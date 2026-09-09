"""Container side of the issue-tracker experiment: the `gh` command, the
`/etc/hosts` guard, and the prompt block that tells the agent about them.

Nothing here decides what may be served -- that is the gateway's job
(gh_gateway.py), on the host, where the agent cannot reach it. This module only
gets a client into the container and points it at the right instance.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

# POSIX sh, and curl -> wget -> bash for transport: ~37 of the 731 base images
# are golang:*-alpine, which have neither python3 nor bash, but busybox always
# brings `wget`. Nothing here may assume more than that.
CLIENT = r"""#!/bin/sh
# gh -- read this repository's GitHub issue tracker as it stood at the commit
# checked out in /app. Served by the benchmark harness: this repository only,
# nothing created after that commit. Read-only; there is no GitHub access here.
GH_URL='__URL__'
GH_INSTANCE='__INSTANCE__'

usage() {
    cat <<'EOF'
usage:
  gh search [issues|prs] <words...> [--limit N]
                         search issues and pull requests (qualifiers like
                         is:issue, is:pr and in:title also work)
  gh show <number>       one issue or pull request, with its comments
  gh diff <number>       the diff of a pull request, when the run enables it
EOF
}

urlencode() {
    s=$1
    out=''
    while [ -n "$s" ]; do
        c=${s%"${s#?}"}
        s=${s#?}
        case $c in
            [a-zA-Z0-9.~_-]) out="$out$c" ;;
            *) out="$out$(printf '%%%02X' "'$c")" ;;
        esac
    done
    printf '%s' "$out"
}

fetch() {
    url="$GH_URL$1"
    if command -v curl >/dev/null 2>&1; then
        curl -sS --max-time 120 "$url"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O - --timeout=120 "$url"
    elif [ -x /bin/bash ]; then
        /bin/bash -c '
            hp=${1#http://}; h=${hp%%/*}; p=${h#*:}; h=${h%%:*}; path=/${hp#*/}
            exec 3<>"/dev/tcp/$h/$p" || exit 1
            printf "GET %s HTTP/1.0\r\nHost: %s\r\nConnection: close\r\n\r\n" "$path" "$h" >&3
            sed "1,/^\r*$/d" <&3
        ' _ "$url"
    else
        echo "gh: no curl, wget or bash available to reach the tracker" >&2
        return 1
    fi
}

cmd=${1:-}
[ -n "$cmd" ] || { usage; exit 2; }
shift
case $cmd in
    search)
        # Models reach for a CLI shape that does not exist here -- a leading
        # `issues`/`prs` word and `--limit N` were both observed in the first
        # smoke run, where they went through as literal search terms and quietly
        # cost recall. Absorb them instead of searching for them.
        terms=''
        limit=''
        case ${1:-} in
            issues|issue) terms='is:issue'; shift ;;
            prs|pr|pulls) terms='is:pr'; shift ;;
        esac
        while [ $# -gt 0 ]; do
            case $1 in
                --limit|-n) shift; limit=${1:-} ;;
                --limit=*) limit=${1#--limit=} ;;
                -*) : ;;
                *) terms="$terms $1" ;;
            esac
            shift
        done
        terms=${terms# }
        [ -n "$terms" ] || { echo "gh search: nothing to search for" >&2; exit 2; }
        url="/search?instance=$(urlencode "$GH_INSTANCE")&q=$(urlencode "$terms")"
        case $limit in [0-9]*) url="$url&limit=$limit" ;; esac
        fetch "$url"
        ;;
    show)
        [ $# -eq 1 ] || { echo "gh show: expected one number" >&2; exit 2; }
        fetch "/thread?instance=$(urlencode "$GH_INSTANCE")&n=$(urlencode "$1")"
        ;;
    diff)
        [ $# -eq 1 ] || { echo "gh diff: expected one number" >&2; exit 2; }
        fetch "/diff?instance=$(urlencode "$GH_INSTANCE")&n=$(urlencode "$1")"
        ;;
    -h|--help|help) usage ;;
    *) echo "gh: unknown command '$cmd'" >&2; usage; exit 2 ;;
esac
"""

# Appended to the agent config's instance_template. `gh_cutoff` / `gh_budget`
# arrive as jinja vars from agent.run(); under StrictUndefined the block and the
# kwargs must be added together, so the feature cannot be half-enabled.
PROMPT_BLOCK = """
<issue_tracker>
## Issue tracker

This repository's GitHub issue tracker is available through the `gh` command.
It serves **only** this repository, and **only** issues, pull requests and
comments created before {{ gh_cutoff }} -- the commit currently checked out in
/app. Nothing written after that point exists as far as this task is concerned.

  gh search [issues|prs] <words...> [--limit N]
                         search issues and pull requests (is:issue, is:pr and
                         in:title also work; default 30 results)
  gh show <number>       one thread with its comments
  gh diff <number>       a pull request's diff, when this run enables it

Up to {{ gh_budget }} searches are available for this task; use them when the
codebase alone leaves the intended behaviour ambiguous. This is background
material, not a specification -- the PR description above remains the task.
</issue_tracker>
"""

BLOCKED_HOSTS = ["api.github.com"]
# Deliberately just the API host. enroot shares the host network namespace, so
# the container can reach the internet; null-routing github.com wholesale would
# also break `npm install` / `go get` of dependencies hosted there, which would
# change the environment for the treatment arm only and confound the A/B. The
# request log and the trajectories are what catch anything that gets past this.


def install_client(env, base_url: str, instance_id: str) -> None:
    """Write /usr/local/bin/gh into the container rootfs.

    Base64 because the script travels through `sh -c` and a heredoc would have
    to survive two levels of quoting.
    """
    script = CLIENT.replace("__URL__", base_url).replace("__INSTANCE__", instance_id)
    payload = base64.b64encode(script.encode()).decode()
    out = env.execute(
        {
            "command": f"printf %s '{payload}' | base64 -d > /usr/local/bin/gh"
            " && chmod 755 /usr/local/bin/gh && /usr/local/bin/gh --help > /dev/null"
            " && echo gh-installed"
        }
    )
    if out["returncode"] != 0 or "gh-installed" not in out["output"]:
        raise RuntimeError(f"could not install the gh client in the container: {out['output']}")


def write_hosts(dest: Path, sysconf: Path | None, blocked: list[str] | None = None) -> Path:
    """A /etc/hosts that null-routes the GitHub API.

    Built on top of the sysconf mirror's file rather than replacing it: that one
    exists to keep `localhost` IPv4-only, without which servers in the benchmark
    images are unreachable from their own test suites.
    """
    base = "127.0.0.1\tlocalhost\n::1\t\tip6-localhost ip6-loopback\n"
    if sysconf and (source := sysconf / "etc" / "hosts").exists():
        base = source.read_text()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(base + "".join(f"127.0.0.1\t{host}\n" for host in (blocked or BLOCKED_HOSTS)))
    return dest


def register(base_url: str, payload: dict, timeout: int = 300) -> dict:
    """Register the instance with the gateway; it resolves the fix PR and
    verifies the cutoff against upstream before agreeing to serve anything."""
    request = urllib.request.Request(
        f"{base_url}/register",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"gateway refused {payload['instance_id']}: {e.read().decode().strip()}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"gateway at {base_url} is unreachable: {e}") from e
