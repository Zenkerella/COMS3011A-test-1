"""Author identity handling: mailmap delegation and manual merges."""
from __future__ import annotations

from helpers import T0
from rat.authors import (AuthorState, build_author_state, load_mailmap_map,
                         sanitize_merge_groups)


# --------------------------------------------------------------------------- #
# Pure unit tests on build_author_state
# --------------------------------------------------------------------------- #
def test_clustering_by_mailmap_only():
    raw = ["A <a@x>", "A2 <a2@x>", "B <b@x>", "A <a@x>"]
    state = build_author_state(raw, {"A2 <a2@x>": "A <a@x>"}, [])
    assert len(state.authors) == 2
    assert state.author_ids.tolist() == [0, 0, 1, 0]
    a = state.authors[0]
    assert a["name"] == "A" and a["commits"] == 3
    assert a["aliases"][0]["commits"] == 2          # raw "A <a@x>" twice
    assert a["mailmapped"] is True and a["merged"] is True
    assert a["keys"] == ["A <a@x>"]               # canonical key only
    assert {row["name"] for row in a["aliases"]} == {"A", "A2"}
    b = state.authors[1]
    assert b["mailmapped"] is False and b["merged"] is False


def test_manual_merge_groups_union():
    raw = ["A <a@x>", "A <a@x>", "A <a@x>", "B <b@x>", "C <c@x>"]
    state = build_author_state(raw, {}, [["B <b@x>", "A <a@x>"], ["C <c@x>"]])
    # the singleton group is ignored; A and B merge into one author
    assert len(state.authors) == 2
    merged = state.authors[0]
    assert merged["name"] == "A" and merged["commits"] == 4
    assert merged["keys"] == ["A <a@x>", "B <b@x>"]
    assert merged["merged"] is True and merged["mailmapped"] is False
    assert state.author_ids.tolist() == [0, 0, 0, 0, 1]


def test_merge_group_with_unknown_keys_is_ignored():
    raw = ["A <a@x>", "B <b@x>"]
    state = build_author_state(raw, {}, [["A <a@x>", "ghost <g@x>"]])
    assert len(state.authors) == 2                  # ghost dropped -> singleton


def test_empty_history():
    state = build_author_state([], {}, [])
    assert isinstance(state, AuthorState)
    assert state.authors == []
    assert state.author_ids.size == 0


def test_sanitize_merge_groups():
    known = {"A <a@x>", "B <b@x>", "C <c@x>"}
    groups = [
        ["A <a@x>", "B <b@x>"],              # kept
        ["A <a@x>"],                         # singleton -> dropped
        ["A <a@x>", "ghost <g@x>"],          # one unknown -> singleton -> dropped
        ["A <a@x>", "B <b@x>", "B <b@x>"],   # duplicates removed, kept
        ["B <b@x>", "C <c@x>", "ghost <g@x>"],  # unknown dropped, still valid
        "not-a-list",                        # wrong type -> dropped
    ]
    assert sanitize_merge_groups(groups, known) == [
        ["A <a@x>", "B <b@x>"],
        ["A <a@x>", "B <b@x>"],
        ["B <b@x>", "C <c@x>"],
    ]
    assert sanitize_merge_groups(None, known) == []


# --------------------------------------------------------------------------- #
# Integration with the scripted repositories
# --------------------------------------------------------------------------- #
def test_mailmap_repo_merges_alt_identity(mm):
    assert mm.mailmap == {"Alice Alt <alice@work.example>": "Alice <alice@example.com>"}
    state = mm.state
    assert len(state.authors) == 3
    by_name = {a["name"]: a for a in state.authors}
    alice = by_name["Alice"]
    assert alice["commits"] == 4
    assert alice["mailmapped"] is True and alice["merged"] is True
    assert [a["commits"] for a in alice["aliases"]] == [2, 2]
    # c1..c10 chronological: Alice, Bob, Carol, Alice, Bob, Carol, Alice, Bob, Carol, Alice
    assert state.author_ids.tolist() == [0, 1, 2, 0, 1, 2, 0, 1, 2, 0]


def test_plain_repo_keeps_identities_separate(plain):
    assert plain.mailmap == {}
    assert len(plain.state.authors) == 4
    by_name = {a["name"]: a for a in plain.state.authors}
    assert set(by_name) == {"Alice", "Alice Alt", "Bob", "Carol"}
    assert by_name["Alice"]["commits"] == 2
    assert by_name["Alice Alt"]["commits"] == 2
    assert by_name["Alice"]["mailmapped"] is False
    assert by_name["Alice"]["merged"] is False


def test_plain_repo_manual_merge_unions_commits(plain):
    """Merging Alice + Alice Alt must reproduce the mailmap result exactly:
    added 9 / removed 2 / churn 11 over 4 commits (2 + 2)."""
    alice_key = next(a["keys"][0] for a in plain.state.authors if a["name"] == "Alice")
    alt_key = next(a["keys"][0] for a in plain.state.authors if a["name"] == "Alice Alt")
    merged = build_author_state(plain.data.raw_ids, plain.mailmap,
                                [[alice_key, alt_key]], version=1)
    assert merged.version == 1
    assert len(merged.authors) == 3
    by_name = {a["name"]: a for a in merged.authors}
    assert set(by_name) == {"Alice", "Bob", "Carol"}
    alice = by_name["Alice"]
    assert alice["commits"] == 4
    assert alice["keys"] == sorted(["Alice <alice@example.com>",
                                    "Alice Alt <alice@work.example>"])
    assert alice["merged"] is True


def test_load_mailmap_map_on_plain_repo_is_empty(plain):
    assert load_mailmap_map(plain.git, "master", plain.data.raw_ids) == {}


def test_has_mailmap_detection(mm, plain):
    assert mm.git.has_mailmap("master") is True
    assert plain.git.has_mailmap("master") is False
    assert mm.data.mailmap_present is True
    assert plain.data.mailmap_present is False


def test_commit_window_uses_committer_dates(mm):
    """Sanity note for the brief: ranges use committer timestamps."""
    assert int(mm.data.times[0]) == T0
    assert int(mm.data.times[-1]) == T0 + 10 * 3600
