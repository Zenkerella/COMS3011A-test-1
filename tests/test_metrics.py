"""Metric engine tests.

Two complementary layers:

1. Hand-computed expectations for the scripted history (protects against
   the naive reference being wrong in the same way as the engine).
2. A full cross-check of ``run_query`` against ``helpers.naive_query`` -
   an independent implementation that re-reads git with one subprocess per
   commit and recomputes everything in plain python loops - across commit
   sets (all/range/list), author filters and object scopes.

Hand-derived totals for the mailmap repo (all commits, repo scope):
    added 24, removed 3, churn 27, growth 21, 8 commits with churn > 0\
    modificationFrequency 0.8, churnRate 2.7
"""
from __future__ import annotations

import pytest

from helpers import (STEP, T0, author_name, naive_history, naive_query,
                     naive_select)
from rat.metrics import QueryRequest, run_query

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _author_ids(state, names: list[str]) -> list[int]:
    wanted = set(names)
    ids = [a["id"] for a in state.authors if a["name"] in wanted]
    assert len(ids) == len(wanted), f"unknown author(s) in {names}"
    return sorted(ids)


def _run(mm_data, state, *, mode="all", from_ts=None, to_ts=None, shas=None,
         author_names=None, scope=None, buckets=64, top=500) -> dict:
    if scope:
        path_type, path = scope
    else:
        path = path_type = None
    req = QueryRequest(
        ref="master",
        commit_mode=mode,
        from_ts=from_ts,
        to_ts=to_ts,
        shas=list(shas or []),
        author_ids=_author_ids(state, author_names) if author_names else [],
        path=path,
        path_type=path_type,
        top=top,
        buckets=buckets,
    )
    return run_query(mm_data, state, req)


def _expected_dirs(scope, naive_dirs: dict) -> dict:
    """Naive dir rows minus the dirs production deliberately excludes."""
    if scope is None:
        return {d: v for d, v in naive_dirs.items() if d != ""}
    kind, val = scope
    if kind == "dir":
        if val == "":
            return {d: v for d, v in naive_dirs.items() if d != ""}
        return {d: v for d, v in naive_dirs.items() if d.startswith(val + "/")}
    return {}


def _check(naive, result, s_idx, scope):
    """Assert equality between a production result and the naive reference."""
    exp = naive_query(naive, s_idx, scope)
    label = f"scope={scope} selection={len(s_idx)}"

    assert result["selection"]["commits"] == exp["selectionSize"], label
    got_m = result["metrics"]
    exp_m = exp["metrics"]
    assert got_m["added"] == exp_m["added"], label
    assert got_m["removed"] == exp_m["removed"], label
    assert got_m["growth"] == exp_m["growth"], label
    assert got_m["churn"] == exp_m["churn"], label
    assert got_m["modifications"] == exp_m["modifications"], label
    assert got_m["modificationFrequency"] == pytest.approx(exp_m["modificationFrequency"]), label
    assert got_m["churnRate"] == pytest.approx(exp_m["churnRate"]), label

    # ---- authors ----
    got_auth = {row["name"]: row for row in result["authors"]}
    exp_auth = {author_name(k): v for k, v in exp["authors"].items()}
    if scope is not None and not (scope[0] == "dir" and scope[1] == ""):
        # at a file / non-root dir scope production lists only authors with
        # real activity on the object (mirror of rat.metrics object_scoped)
        exp_auth = {k: v for k, v in exp_auth.items() if v["added"] + v["removed"] > 0}
    assert set(got_auth) == set(exp_auth), label
    total_churn = exp_m["churn"]
    for name, row in got_auth.items():
        want = exp_auth[name]
        assert row["commits"] == want["commits"], f"{label} author={name}"
        assert row["added"] == want["added"], f"{label} author={name}"
        assert row["removed"] == want["removed"], f"{label} author={name}"
        assert row["churn"] == want["added"] + want["removed"], f"{label} author={name}"
        assert row["modifications"] == len(want["mods"]), f"{label} author={name}"
        want_own = (row["churn"] / total_churn) if total_churn else 0.0
        assert row["ownership"] == pytest.approx(want_own), f"{label} author={name}"

    # ---- directories: production rows are exactly the active dirs ----
    want_dirs = _expected_dirs(scope, exp["dirs"])
    got_dirs = {row["path"]: row for row in result["directories"]}
    assert set(got_dirs) == set(want_dirs), f"{label} dirs {sorted(got_dirs)} vs {sorted(want_dirs)}"
    for path, row in got_dirs.items():
        want = want_dirs[path]
        assert row["added"] == want["added"], f"{label} dir={path}"
        assert row["removed"] == want["removed"], f"{label} dir={path}"
        assert row["modifications"] == len(want["mods"]), f"{label} dir={path}"

    # ---- files: every naive file must appear with equal numbers; extra
    # production rows may only be zero-churn placeholders ----
    got_files = {row["path"]: row for row in result["files"]}
    for path, want in exp["files"].items():
        assert path in got_files, f"{label} file={path} missing"
        row = got_files[path]
        assert row["added"] == want["added"], f"{label} file={path}"
        assert row["removed"] == want["removed"], f"{label} file={path}"
        assert row["modifications"] == len(want["mods"]), f"{label} file={path}"
    for path, row in got_files.items():
        if path not in exp["files"]:
            assert row["churn"] == 0, f"{label} unexpected active file={path}"

    return exp


# --------------------------------------------------------------------------- #
# Hand-computed expectations (the scripted history is fully deterministic)
# --------------------------------------------------------------------------- #
def test_hand_computed_repo_metrics(mm):
    result = _run(mm.data, mm.state)
    m = result["metrics"]
    assert (m["added"], m["removed"], m["growth"], m["churn"]) == (24, 3, 21, 27)
    assert m["modifications"] == 8
    assert m["modificationFrequency"] == pytest.approx(0.8)
    assert m["churnRate"] == pytest.approx(2.7)
    assert result["selection"]["commits"] == 10
    assert result["selection"]["totalCommits"] == 10
    assert result["selection"]["firstCommit"]["subject"] == "Initial import"
    assert result["selection"]["lastCommit"]["subject"] == "Polish main"


def test_hand_computed_author_metrics(mm):
    result = _run(mm.data, mm.state)
    rows = {row["name"]: row for row in result["authors"]}
    assert set(rows) == {"Alice", "Bob", "Carol"}       # mailmap merged Alice Alt

    alice = rows["Alice"]
    assert (alice["added"], alice["removed"], alice["churn"]) == (10, 2, 12)
    assert alice["commits"] == 4 and alice["modifications"] == 3
    assert alice["ownership"] == pytest.approx(12 / 27)
    assert alice["mailmapped"] is True and alice["merged"] is True

    bob = rows["Bob"]
    assert (bob["added"], bob["removed"], bob["churn"]) == (7, 0, 7)
    assert bob["commits"] == 3 and bob["modifications"] == 2
    assert bob["ownership"] == pytest.approx(7 / 27)

    carol = rows["Carol"]
    assert (carol["added"], carol["removed"], carol["churn"]) == (7, 1, 8)
    assert carol["commits"] == 3 and carol["modifications"] == 3
    assert carol["ownership"] == pytest.approx(8 / 27)


def test_hand_computed_dir_and_file_rows(mm):
    result = _run(mm.data, mm.state)
    dirs = {row["path"]: row for row in result["directories"]}
    assert set(dirs) == {"src", "src/inner", "docs", "lib"}   # assets is 0-churn
    src = dirs["src"]
    assert (src["added"], src["removed"], src["churn"]) == (17, 2, 19)
    assert src["modifications"] == 5 and src["modificationFrequency"] == pytest.approx(0.5)
    assert dirs["src/inner"]["added"] == 5 and dirs["src/inner"]["modifications"] == 1
    assert dirs["docs"]["added"] == 4 and dirs["docs"]["modifications"] == 2
    assert dirs["lib"]["added"] == 1 and dirs["lib"]["modifications"] == 1

    files = {row["path"]: row for row in result["files"]}
    main = files["src/main.c"]
    assert (main["added"], main["removed"], main["churn"]) == (6, 2, 8)
    assert main["modifications"] == 3 and main["atTip"] is True
    assert files["root.txt"]["added"] == 1 and files["root.txt"]["removed"] == 1
    assert files["root.txt"]["atTip"] is False
    assert "assets/logo.bin" not in files                # binary: not measured
    assert files["src/util.h"]["modifications"] == 1     # pre-rename records stay
    assert files["src/util.h"]["atTip"] is False


# --------------------------------------------------------------------------- #
# Cross-check against the naive reference
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def mm_naive(mm):
    return naive_history(mm.path, "master", mailmap=True)


@pytest.fixture(scope="session")
def plain_naive(plain):
    return naive_history(plain.path, "master", mailmap=False)


SCENARIOS = [
    # name, query kwargs, scope
    ("all/repo", {}, None),
    ("all/dir-src", {}, ("dir", "src")),
    ("all/dir-src-inner", {}, ("dir", "src/inner")),
    ("all/dir-docs", {}, ("dir", "docs")),
    ("all/dir-root", {}, ("dir", "")),
    ("all/file-main", {}, ("file", "src/main.c")),
    ("all/file-deleted", {}, ("file", "root.txt")),
    ("all/file-pure-rename", {}, ("file", "src/util.h")),
    ("range/mid-repo", {"mode": "range", "from_ts": T0 + 1 * STEP, "to_ts": T0 + 8 * STEP}, None),
    ("range/tail", {"mode": "range", "from_ts": T0 + 6 * STEP}, None),
    ("range/empty", {"mode": "range", "from_ts": T0 + 99 * STEP}, None),
    ("range/file-after-delete", {"mode": "range", "from_ts": T0 + 6 * STEP}, ("file", "root.txt")),
    ("range/dir-src", {"mode": "range", "from_ts": T0 + 7 * STEP}, ("dir", "src")),
    ("author-bob/all", {"author_names": ["Bob"]}, None),
    ("author-alice+carol/range", {"author_names": ["Alice", "Carol"],
                                  "mode": "range", "from_ts": T0 + 2 * STEP}, None),
    ("author-carol/dir-src", {"author_names": ["Carol"]}, ("dir", "src")),
    ("author-bob/file-main", {"author_names": ["Bob"]}, ("file", "src/main.c")),
]


@pytest.mark.parametrize("name,kwargs,scope", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_cross_check(mm, mm_naive, name, kwargs, scope):
    kwargs = dict(kwargs)
    mode = kwargs.get("mode", "all")
    s_idx = naive_select(mm_naive, **kwargs)
    result = _run(mm.data, mm.state, scope=scope, **kwargs)
    _check(mm_naive, result, s_idx, scope)


@pytest.mark.parametrize("name,kwargs,scope", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_cross_check_plain_repo(plain, plain_naive, name, kwargs, scope):
    """Same scenarios on the repository WITHOUT a mailmap."""
    kwargs = dict(kwargs)
    s_idx = naive_select(plain_naive, **kwargs)
    result = _run(plain.data, plain.state, scope=scope, **kwargs)
    _check(plain_naive, result, s_idx, scope)


def test_list_mode_selection_and_unknown_shas(mm, mm_naive):
    shas = [mm.commits["c1"], mm.commits["c6"], mm.commits["c10"], "f" * 40]
    result = _run(mm.data, mm.state, mode="list", shas=shas)
    assert result["selection"]["commits"] == 3           # unknown sha ignored
    assert result["metrics"]["added"] == 8               # 7 + 0 + 1
    assert result["metrics"]["removed"] == 2             # 0 + 1 + 1
    assert result["metrics"]["modifications"] == 3
    assert result["metrics"]["modificationFrequency"] == pytest.approx(1.0)
    s_idx = naive_select(mm_naive, mode="list", shas=shas)
    _check(mm_naive, result, s_idx, None)


def test_empty_selection_yields_zeroes(mm, mm_naive):
    result = _run(mm.data, mm.state, mode="list", shas=["e" * 40])
    assert result["selection"]["commits"] == 0
    assert result["selection"]["firstCommit"] is None
    assert result["metrics"] == {"added": 0, "removed": 0, "growth": 0, "churn": 0,
                                 "modifications": 0, "modificationFrequency": 0.0,
                                 "churnRate": 0.0}
    assert result["series"] == []
    assert result["authors"] == []
    assert result["files"] == [] and result["directories"] == []
    _check(mm_naive, result, [], None)


def test_root_dir_scope_equals_repo_scope(mm):
    repo = _run(mm.data, mm.state)
    root = _run(mm.data, mm.state, scope=("dir", ""))
    assert repo["metrics"] == root["metrics"]
    assert [d["path"] for d in repo["directories"]] == [d["path"] for d in root["directories"]]


def test_file_scope_drilldown(mm):
    """File scope restricts records to that path only."""
    result = _run(mm.data, mm.state, scope=("file", "src/main.c"))
    m = result["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (6, 2, 8)
    assert m["modifications"] == 3
    assert [f["path"] for f in result["files"]] == ["src/main.c"]
    assert result["directories"] == []                   # no directory rows for a file scope
    rows = {r["name"]: r for r in result["authors"]}
    assert set(rows) == {"Alice"}                        # only Alice & Alice Alt touched it


def test_modification_frequency_denominator_is_selection_size(mm):
    """frequency = modifications / |S|; |S| counts every selected commit."""
    result = _run(mm.data, mm.state, mode="range", from_ts=T0 + 1 * STEP, to_ts=T0 + 8 * STEP)
    assert result["selection"]["commits"] == 6
    assert result["metrics"]["modifications"] == 4
    assert result["metrics"]["modificationFrequency"] == pytest.approx(4 / 6)
    assert result["metrics"]["churnRate"] == pytest.approx(15 / 6)


def test_series_is_consistent_with_totals(mm):
    result = _run(mm.data, mm.state, buckets=64)
    series = result["series"]
    assert series                                          # non-empty
    assert sum(s["added"] for s in series) == result["metrics"]["added"]
    assert sum(s["removed"] for s in series) == result["metrics"]["removed"]
    assert sum(s["commits"] for s in series) == result["selection"]["commits"]
    assert series[-1]["cumulativeGrowth"] == result["metrics"]["growth"]
    assert [s["t"] for s in series] == sorted(s["t"] for s in series)


def test_series_bucket_clamping(mm):
    few = _run(mm.data, mm.state, buckets=3)["series"]
    assert len(few) == 3
    many = _run(mm.data, mm.state, buckets=1000)["series"]
    assert len(many) == 10                                 # cannot exceed |S|
