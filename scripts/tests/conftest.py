"""Shared fixtures for the repository's release-script tests."""

import os
from pathlib import Path
import shutil
import sys

import pytest


@pytest.fixture(scope="session")
def bash_executable():
    if sys.platform != "win32":
        return "bash"

    # Windows also ships a WSL launcher named bash.exe. Locate Git Bash next
    # to Git instead of relying on PATH ordering in a Python child process.
    git = shutil.which("git")
    candidates = []
    if git:
        git_dir = Path(git).parent
        candidates.extend(
            (
                git_dir / "bash.exe",
                git_dir.parent / "bin/bash.exe",
                git_dir.parent.parent / "bin/bash.exe",
            )
        )
    for name in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(name)
        if program_files:
            candidates.append(Path(program_files) / "Git/bin/bash.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    pytest.fail("Git Bash is required on Windows; the WSL bash.exe cannot run these tests")
