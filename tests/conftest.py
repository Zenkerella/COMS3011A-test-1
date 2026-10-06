"""Shared pytest harness for the RAT test-suite.

Order matters here: ``RAT_DATA_DIR`` is redirected to a temporary directory
*before* any ``rat`` module is imported, because ``rat.config`` resolves its
paths once at import time.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# --------------------------------------------------------------------------- #
# Redirect all runtime state into a throw-away directory BEFORE rat imports.
# --------------------------------------------------------------------------- #
_TMP_ROOT = Path(os.environ.get("RAT_TEST_TMP") or tempfile.mkdtemp(prefix="rat-tests-"))
_TMP_ROOT = _TMP_ROOT.resolve()

# Wipe state left behind by earlier runs (``RAT_TEST_KEEP=1`` keeps the root
# alive between runs) so every session starts from a clean slate.
for _stale in ("data", "work", "cache"):
    shutil.rmtree(_TMP_ROOT / _stale, ignore_errors=True)
(_TMP_ROOT / "data").mkdir(parents=True, exist_ok=True)
os.environ["RAT_DATA_DIR"] = str(_TMP_ROOT / "data")

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))          # tests/   (helpers)
sys.path.insert(0, str(_HERE.parents[1]))      # project root (rat)

import pytest  # noqa: E402

from helpers import build_script_repo, zip_tree  # noqa: E402
from rat.authors import build_author_state, load_mailmap_map  # noqa: E402
from rat.gitrepo import GitRepo  # noqa: E402
from rat.numstat import build_and_cache  # noqa: E402


def _pack(path: Path, commits: dict, cache_name: str) -> SimpleNamespace:
    """Parse the fixture repo exactly like the store does after ingestion."""
    git = GitRepo(gitdir=path / ".git", worktree=path)
    cache_dir = _TMP_ROOT / "cache" / cache_name
    data = build_and_cache(git, "master", cache_dir)
    mailmap = load_mailmap_map(git, "master", data.raw_ids)
    state = build_author_state(data.raw_ids, mailmap, [])
    return SimpleNamespace(path=path, commits=commits, git=git, data=data,
                           state=state, mailmap=mailmap, cache_dir=cache_dir,
                           tmp_root=_TMP_ROOT)


@pytest.fixture(scope="session")
def mm() -> SimpleNamespace:
    """The scripted repository WITH a .mailmap (Alice Alt -> Alice)."""
    path = _TMP_ROOT / "work" / "mm-repo"
    commits = build_script_repo(path, mailmap=True)
    return _pack(path, commits, "mm")


@pytest.fixture(scope="session")
def plain() -> SimpleNamespace:
    """The same scripted history without a .mailmap."""
    path = _TMP_ROOT / "work" / "plain-repo"
    commits = build_script_repo(path, mailmap=False)
    return _pack(path, commits, "plain")


@pytest.fixture(scope="session")
def mm_zip(mm) -> Path:
    """A user-style zip upload of the mailmap repository."""
    return zip_tree(mm.path, _TMP_ROOT / "mm-repo.zip", "mm-repo")


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup():
    yield
    if not os.environ.get("RAT_TEST_KEEP"):
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
