"""Site defaults shared by the single-instance entry points.

``config.sh`` is the source of truth for the shell layer; these mirror it for the
Python CLIs so ``--help`` and hand-run instances agree with what the arrays use.
"""

import getpass
import os
from pathlib import Path

# Staged .sqsh images. Matches config.sh's SWEBP_IMAGES_DIR default, which is
# per-user because scratch is per-user.
DEFAULT_IMAGES_DIR = Path(
    os.environ.get("SWEBP_IMAGES_DIR") or f"/sc/scratch/{getpass.getuser()}/swebp/images"
)

# DockerHub account owning the upstream SWE-Bench-Pro instance images.
DEFAULT_DOCKERHUB_USERNAME = "jefzda"
