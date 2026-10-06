"""Parser + index tests against the scripted repository.

Expected numbers below are hand-derived from the fixture script in
``helpers.build_script_repo`` (verified against real git output):

======== ==== ===== ===== ===============================================
tag      add  rem   paths / notes
======== ==== ===== ===== ===============================================
c1       7    0     .mailmap, root.txt, src/main.c, docs/readme.md
c2       6    0     src/util.c, src/util.h
c3f      5    0     src/inner/deep.c
c4       2    1     src/main.c
c5       0    0     binary only - NOT measured (absent from records)
c6       0    1     root.txt (deletion recorded as removals)
c7       0    0     pure rename - no record at all
c8       1    0     lib/util.c (rename-with-edit: new path only)
c9       2    0     docs/readme.md
c10      1    1     src/main.c
m1       -    -     merge commit - excluded entirely
======== ==== ===== ===== ===============================================
"""
from __future__ import annotations

import pytest

from helpers import STEP, T0, build_tiny_repo
from rat.authors import build_author_state
from rat.gitrepo import GitRepo
from rat.metrics import QueryRequest, run_query
from rat.numstat import _split_stats, build_and_cache, load_cache, parse_numstat_bytes

NON_MERGE_TAGS = ["c1", "c2", "c3f", "c4", "c5", "c6", "c7", "c8", "c9", "c10"]

EXPECTED_CHANGES = {
    "c1": {".mailmap": (1, 0), "root.txt": (1, 0), "src/main.c": (3, 0), "docs/readme.md": (2, 0)},
    "c2": {"src/util.c": (4, 0), "src/util.h": (2, 0)},
    "c3f": {"src/inner/deep.c": (5, 0)},
    "c4": {"src/main.c": (2, 1)},
    "c5": {},
    "c6": {"root.txt": (0, 1)},
    "c7": {},
    "c8": {"lib/util.c": (1, 0)},
    "c9": {"docs/readme.md": (2, 0)},
    "c10": {"src/main.c": (1, 1)},
}


def _commit_changes(data) -> dict[str, dict[str, tuple[int, int]]]:
    """sha -> {path: (added, removed)} rebuilt from the parsed arrays."""
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for idx, pid, added, removed in zip(data.rec_commit.tolist(), data.rec_path.tolist(),
                                        data.rec_added.tolist(), data.rec_removed.tolist()):
        sha = data.shas[idx]
        path = data.paths[pid]
        assert path not in out.setdefault(sha, {}), "one record per (commit, path)"
        out[sha][path] = (int(added), int(removed))
    return out


# --------------------------------------------------------------------------- #
# Token-level parsing of raw byte streams
# --------------------------------------------------------------------------- #
def _header(sha: bytes, name=b"Alice", email=b"a@x", ct=T0, subject=b"Init"):
    return b"C\x01" + sha + b"\x01" + name + b"\x01" + email + b"\x01" + \
        str(ct).encode() + b"\x01" + subject + b"\n"


def test_split_stats_variants():
    assert _split_stats(b"3\t1\tsrc/one.c") == (3, 1, b"src/one.c")
    assert _split_stats(b"-\t-\tbin.dat") == (None, None, b"bin.dat")
    assert _split_stats(b"7\t0\t") == (7, 0, b"")           # rename stats
    assert _split_stats(b"not-a-stat") == (None, None, b"")
    assert _split_stats(b"a\tb\tpath.c") == (None, None, b"path.c")  # treated as unmeasurable


def test_parse_empty_stream():
    parsed = parse_numstat_bytes(b"")
    assert parsed["commit_sha"] == []
    assert parsed["rec_commit"] == []
    assert parsed["seen_paths"] == set()
    assert parsed["binary_paths"] == set()


def test_parse_header_merged_with_first_entry():
    """git log -z merges the commit header and the first numstat entry into
    one NUL-delimited token - the trickiest part of the state machine."""
    sha = b"a" * 40
    raw = (
        _header(sha)
        + b"3\t1\tsrc/one.c\x00"
        + b"-\t-\tbin.dat\x00"
        + b"7\t0\t\x00old.c\x00new.c\x00"
        + b"0\t0\t\x00pure.c\x00pure2.c\x00"
    )
    parsed = parse_numstat_bytes(raw)
    assert parsed["commit_sha"] == [sha]
    assert parsed["commit_name"] == [b"Alice"]
    assert parsed["rec_commit"] == [0, 0]        # 0-based commit index
    assert parsed["rec_path"] == [b"src/one.c", b"new.c"]
    assert parsed["rec_added"] == [3, 7]
    assert parsed["rec_removed"] == [1, 0]
    assert b"bin.dat" in parsed["binary_paths"]
    assert parsed["seen_paths"] == {b"src/one.c", b"bin.dat", b"old.c", b"new.c",
                                    b"pure.c", b"pure2.c"}


def test_parse_two_commits_state_does_not_leak():
    sha1, sha2 = b"1" * 40, b"2" * 40
    raw = (
        _header(sha1, subject=b"one")
        + b"2\t0\tf.txt\x00"
        + b"\x00"                                   # empty token between commits
        + _header(sha2, name=b"Bob", email=b"b@x", ct=T0 + STEP, subject=b"two")
        + b"0\t3\tf.txt\x00"
    )
    parsed = parse_numstat_bytes(raw)
    assert parsed["commit_sha"] == [sha1, sha2]
    assert parsed["rec_commit"] == [0, 1]
    assert parsed["rec_added"] == [2, 0]
    assert parsed["rec_removed"] == [0, 3]


def test_parse_empty_commit_bare_header():
    """Empty-diff commits arrive as a bare header token: no trailing
    newline and no entries.  They must still be counted in the history."""
    sha1, sha2 = b"3" * 40, b"4" * 40
    raw = (
        _header(sha1, name=b"Bob", subject=b"noop").rstrip(b"\n")  # bare
        + b"\x00"
        + _header(sha2, subject=b"real")
        + b"5\t0\tf.txt\x00"
    )
    parsed = parse_numstat_bytes(raw)
    assert parsed["commit_sha"] == [sha1, sha2]
    assert parsed["commit_subject"] == [b"noop", b"real"]
    assert parsed["rec_commit"] == [1]              # only the real commit
    assert parsed["rec_added"] == [5]
    assert parsed["rec_removed"] == [0]


# --------------------------------------------------------------------------- #
# Index construction against the real scripted repository
# --------------------------------------------------------------------------- #
def test_commits_are_non_merge_and_chronological(mm):
    data = mm.data
    assert data.ref == "master"
    assert data.n_commits == len(NON_MERGE_TAGS)
    assert set(data.shas) == {mm.commits[t] for t in NON_MERGE_TAGS}
    assert mm.commits["m1"] not in data.sha_index      # merge excluded
    assert data.times.tolist() == [T0 + k * STEP for k in (0, 1, 2, 3, 5, 6, 7, 8, 9, 10)]
    assert data.subjects[0] == "Initial import"
    assert data.subjects[-1] == "Polish main"


def test_records_match_scripted_history(mm):
    got = _commit_changes(mm.data)
    for tag, expected in EXPECTED_CHANGES.items():
        assert got.get(mm.commits[tag], {}) == expected, f"mismatch for {tag}"


def test_rename_rules(mm):
    data = mm.data
    # pure rename: the new name never earns a record (no content delta)...
    assert not data.path_has_records[data.path_id("src/utils.h")]
    # ...the old name keeps only its pre-rename history
    assert data.path_has_records[data.path_id("src/util.h")]
    assert not data.path_at_tip[data.path_id("src/util.h")]
    assert data.path_at_tip[data.path_id("src/utils.h")]
    # rename-with-edit: content delta attributed to the new path only
    assert data.path_has_records[data.path_id("src/util.c")]
    assert data.path_has_records[data.path_id("lib/util.c")]
    assert not data.path_at_tip[data.path_id("src/util.c")]
    assert data.path_at_tip[data.path_id("lib/util.c")]


def test_binary_and_deleted_paths(mm):
    data = mm.data
    logo = data.path_id("assets/logo.bin")
    assert data.path_binary[logo] and data.path_at_tip[logo]
    assert not data.path_has_records[logo]             # binary: not measured
    root = data.path_id("root.txt")
    assert not data.path_at_tip[root] and data.path_has_records[root]
    assert not data.path_binary[root]


def test_directory_index_and_ancestors(mm):
    data = mm.data
    assert data.dirs == ["", "assets", "docs", "lib", "src", "src/inner"]
    assert data.dirs[0] == "" and data.dir_parent[0] == -1

    def chain(path: str) -> list[str]:
        pid = data.path_id(path)
        lo, hi = int(data.anc_ptr[pid]), int(data.anc_ptr[pid + 1])
        return [data.dirs[int(d)] for d in data.anc_ids[lo:hi]]

    assert chain("root.txt") == [""]
    assert chain("src/main.c") == ["", "src"]
    assert chain("src/inner/deep.c") == ["", "src", "src/inner"]  # root first

    lo, hi = data.path_range("src")
    assert [data.paths[i] for i in range(lo, hi)] == [
        "src/inner/deep.c", "src/main.c", "src/util.c", "src/util.h", "src/utils.h"]
    lo, hi = data.path_range("")
    assert (lo, hi) == (0, data.n_paths)


def test_cache_roundtrip(mm):
    """build_and_cache persisted a pickle that load_cache restores intact."""
    import numpy as np

    cached = load_cache(mm.cache_dir, mm.data.ref_sha)
    assert cached is not None
    assert cached.shas == mm.data.shas
    assert cached.paths == mm.data.paths
    assert cached.dirs == mm.data.dirs
    assert np.array_equal(cached.rec_added, mm.data.rec_added)
    assert np.array_equal(cached.rec_removed, mm.data.rec_removed)
    assert np.array_equal(cached.rec_commit, mm.data.rec_commit)
    assert cached.summary()["commits"] == len(NON_MERGE_TAGS)


def test_build_and_cache_requires_valid_ref(mm):
    with pytest.raises(ValueError):
        build_and_cache(mm.git, "no-such-ref", mm.tmp_root / "cache" / "scratch")


def test_empty_diff_commit_is_counted(tmp_path):
    """Regression for real repositories (git.git, redis): a commit whose
    diff is empty must stay in the history - it contributes to |S| but
    nothing else."""
    path = tmp_path / "tiny"
    commits = build_tiny_repo(path)
    git = GitRepo(gitdir=path / ".git", worktree=path)
    data = build_and_cache(git, "master", tmp_path / "cache")

    assert data.n_commits == 3
    assert data.subjects == ["Add a", "Empty commit", "Edit a"]
    empty = data.sha_index[commits["t2"]]
    assert not (data.rec_commit == empty).any()
    assert int(data.rec_added.sum()) == 2 and int(data.rec_removed.sum()) == 1

    state = build_author_state(data.raw_ids, {}, [])
    result = run_query(data, state, QueryRequest(ref="master"))
    assert result["selection"]["commits"] == 3           # |S| counts it
    assert result["metrics"]["modifications"] == 2       # but nothing changed
    assert result["metrics"]["modificationFrequency"] == pytest.approx(2 / 3)
    assert result["metrics"]["churnRate"] == pytest.approx(1.0)
