"""Build per-account enroot credential dirs for authenticated DockerHub pulls.

DockerHub's pull limit is per authenticated account; anonymous pulls are per-IP and
the whole cluster shares one (exhausted) NAT IP, so staging must authenticate. enroot
reads ``${ENROOT_CONFIG_PATH}/.credentials`` and uses the first matching ``machine``
line, so each account needs its own config dir -- ``slurm/stage_image.sh`` selects one
via ``ENROOT_CONFIG_PATH`` and fails over from dh1 to dh2 at the 6h pull cap.

Reads two accounts from mini's ``.env`` (never logs the PAT) and writes
``~/.config/enroot-dh{1,2}/.credentials`` mode 0600. Run once before submitting.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

ENV = Path.home() / ".config/mini-swe-agent/.env"
ACCOUNTS = {
    "enroot-dh1": ("DOCKERHUB_USERNAME", "DOCKERHUB_PAT"),
    "enroot-dh2": ("2nd_DOCKERHUB_USERNAME", "2nd_DOCKERHUB_PAT"),
}


def main() -> None:
    env = dotenv_values(ENV)
    for dirname, (user_key, pat_key) in ACCOUNTS.items():
        user, pat = env.get(user_key), env.get(pat_key)
        if not user or not pat:
            raise SystemExit(f"missing {user_key}/{pat_key} in {ENV}")
        cred = Path.home() / ".config" / dirname / ".credentials"
        cred.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the start so the PAT is never world-readable.
        fd = os.open(cred, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"machine auth.docker.io login {user} password {pat}\n")
            f.write(f"machine registry-1.docker.io login {user} password {pat}\n")
        print(f"wrote {cred}  (login={user})")


if __name__ == "__main__":
    main()
