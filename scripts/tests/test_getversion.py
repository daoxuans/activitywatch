"""The fork's test builds do not depend on upstream tags being pushed to origin."""

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[2]
GETVERSION = ROOT / "scripts/package/getversion.sh"


def test_explicit_fork_build_version_precedes_git_tag_environment():
    env = dict(
        os.environ,
        AW_BUILD_VERSION="v0.14.0.dev-deadbeef",
        GITHUB_REF="refs/tags/v99.0.0",
        GITHUB_REF_NAME="v99.0.0",
    )
    result = subprocess.run(
        ["bash", str(GETVERSION), "--strip-v"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.14.0.dev-deadbeef"
