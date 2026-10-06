"""End-to-end HTTP API tests through Flask's test client.

The module fixture ingests two repositories the same way a user would:

* ``mm-repo``        - uploaded as a ZIP of a working tree (with .git)
* ``plain-clone``    - deep-cloned from a local path (mirror)

Both carry the same scripted 10-commit history (see helpers), so every
expected number here is hand-derived from that script.
"""
from __future__ import annotations

import io
import time
from types import SimpleNamespace

import pytest

from helpers import STEP, T0
from rat.config import DATA_DIR
from server import create_app

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _repo_json(client, rid: str) -> dict:
    resp = client.get(f"/api/repos/{rid}")
    assert resp.status_code == 200
    return resp.get_json()["repo"]


def _wait_ready(client, rid: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        repo = _repo_json(client, rid)
        if repo["status"] == "ready":
            return repo
        if repo["status"] == "error":
            pytest.fail(f"ingestion of {rid} failed: {repo.get('error') or repo.get('message')}")
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {rid} to become ready")


def _metrics(client, rid: str, *, ref="master", mode="all", from_ts=None, to_ts=None,
             shas=None, authors=None, path=None, path_type=None):
    body: dict = {"ref": ref, "commits": {"mode": mode}}
    if from_ts is not None:
        body["commits"]["from"] = from_ts
    if to_ts is not None:
        body["commits"]["to"] = to_ts
    if shas is not None:
        body["commits"]["shas"] = shas
    if authors is not None:
        body["authors"] = authors
    if path is not None:
        body["path"] = path
        if path_type is not None:
            body["pathType"] = path_type
    return client.post(f"/api/repos/{rid}/metrics", json=body)


# --------------------------------------------------------------------------- #
# Module-wide environment (one ingestion for the whole file)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def api_env(mm_zip, plain):
    app = create_app(DATA_DIR)
    client = app.test_client()

    with mm_zip.open("rb") as fh:
        resp = client.post("/api/repos/upload",
                           data={"file": (fh, "mm-repo.zip"), "name": "mm-repo"},
                           content_type="multipart/form-data")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    mm_id = resp.get_json()["repo"]["id"]
    _wait_ready(client, mm_id)

    resp = client.post("/api/repos/clone",
                       json={"url": str(plain.path), "name": "plain-clone"})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    plain_id = resp.get_json()["repo"]["id"]
    _wait_ready(client, plain_id)

    return SimpleNamespace(client=client, mm_id=mm_id, plain_id=plain_id)


# --------------------------------------------------------------------------- #
# ingestion + detail
# --------------------------------------------------------------------------- #
def test_config_endpoint(api_env):
    resp = api_env.client.get("/api/config")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["version"]
    for feature in ("zip", "clone", "multi-repo", "filters", "author-merge", "mailmap", "metrics"):
        assert feature in payload["features"]


def test_create_app_accepts_string_data_dir():
    """``--data DIR`` reaches create_app as a str; it must be coerced (regression)."""
    client = create_app(str(DATA_DIR)).test_client()
    assert client.get("/api/config").status_code == 200


def test_detail_after_zip_ingestion(api_env):
    repo = _repo_json(api_env.client, api_env.mm_id)
    assert repo["status"] == "ready"
    assert repo["sourceType"] == "zip"
    assert repo["defaultRef"] == "master"
    stats = repo["stats"]
    assert stats["commits"] == 10
    assert stats["authors"] == 3                  # mailmap merged Alice Alt
    assert stats["tipFiles"] == 7
    assert stats["dirs"] == 5
    assert stats["mailmapPresent"] is True
    assert stats["binaryFiles"] == 1


def test_detail_after_clone_ingestion(api_env):
    repo = _repo_json(api_env.client, api_env.plain_id)
    assert repo["status"] == "ready"
    assert repo["sourceType"] == "clone"
    assert repo["defaultRef"] == "master"
    assert repo["stats"]["commits"] == 10
    assert repo["stats"]["authors"] == 4          # no mailmap: 4 identities
    assert repo["stats"]["mailmapPresent"] is False


# --------------------------------------------------------------------------- #
# metrics endpoints
# --------------------------------------------------------------------------- #
def test_repository_metrics(api_env):
    result = _metrics(api_env.client, api_env.mm_id)
    assert result.status_code == 200
    payload = result.get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["growth"], m["churn"]) == (24, 3, 21, 27)
    assert m["modifications"] == 8
    assert m["modificationFrequency"] == pytest.approx(0.8)
    assert m["churnRate"] == pytest.approx(2.7)
    sel = payload["selection"]
    assert sel["commits"] == 10 and sel["totalCommits"] == 10
    assert sel["firstCommit"]["subject"] == "Initial import"
    assert sel["lastCommit"]["subject"] == "Polish main"
    assert payload["repo"]["id"] == api_env.mm_id
    assert payload["series"]


def test_plain_repo_metrics(api_env):
    payload = _metrics(api_env.client, api_env.plain_id).get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (23, 3, 26)
    assert m["modifications"] == 8


def test_dir_scope_metrics(api_env):
    resp = _metrics(api_env.client, api_env.mm_id, path="src", path_type="dir")
    assert resp.status_code == 200
    payload = resp.get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (17, 2, 19)
    assert m["modifications"] == 5
    assert payload["object"] == {"path": "src", "type": "dir"}
    dirs = {row["path"] for row in payload["directories"]}
    assert dirs == {"src/inner"}                   # strictly inside the scope


def test_file_scope_metrics(api_env):
    payload = _metrics(api_env.client, api_env.mm_id, path="src/main.c", path_type="file").get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (6, 2, 8)
    assert m["modifications"] == 3
    assert m["modificationFrequency"] == pytest.approx(0.3)
    assert [f["path"] for f in payload["files"]] == ["src/main.c"]
    assert payload["files"][0]["atTip"] is True


def test_file_scopes_renames_and_deletions(api_env):
    client = api_env.client
    # pure-renamed-away file keeps its historical records but is not at tip
    payload = _metrics(client, api_env.mm_id, path="src/util.h").get_json()
    assert payload["metrics"]["added"] == 2
    assert payload["object"]["type"] == "file"     # inferred from the path
    assert payload["files"][0]["atTip"] is False
    # rename-with-edit lands on the new path
    payload = _metrics(client, api_env.mm_id, path="lib/util.c").get_json()
    assert payload["metrics"]["added"] == 1
    assert payload["files"][0]["atTip"] is True
    # binary file stays unmeasured but visible in the path index
    resp = _metrics(client, api_env.mm_id, path="assets/logo.bin", path_type="file")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["metrics"]["churn"] == 0
    paths = client.get(f"/api/repos/{api_env.mm_id}/paths",
                       query_string={"q": "logo", "limit": 50}).get_json()
    assert paths["files"] == [{"path": "assets/logo.bin", "type": "file"}]


def test_range_mode(api_env):
    payload = _metrics(api_env.client, api_env.mm_id,
                       mode="range",
                       from_ts=T0 + 1 * STEP, to_ts=T0 + 8 * STEP).get_json()
    sel = payload["selection"]
    assert sel["commits"] == 6                     # [from, to) on committer dates
    assert sel["from"] and sel["to"]
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (13, 2, 15)
    assert m["modifications"] == 4
    assert m["modificationFrequency"] == pytest.approx(4 / 6)


def test_list_mode(api_env):
    client = api_env.client
    page = client.get(f"/api/repos/{api_env.mm_id}/commits",
                      query_string={"limit": 500}).get_json()
    by_subject = {c["subject"]: c["sha"] for c in page["commits"]}
    shas = [by_subject["Initial import"], by_subject["Drop root note"],
            by_subject["Polish main"], "f" * 40]   # unknown sha must be ignored
    payload = _metrics(client, api_env.mm_id, mode="list", shas=shas).get_json()
    assert payload["selection"]["commits"] == 3
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (8, 2, 10)
    assert m["modifications"] == 3
    assert m["modificationFrequency"] == pytest.approx(1.0)


def test_range_plus_author_filter(api_env):
    client = api_env.client
    authors = client.get(f"/api/repos/{api_env.mm_id}/authors").get_json()
    bob = next(a for a in authors["authors"] if a["name"] == "Bob")
    payload = _metrics(client, api_env.mm_id, authors=[bob["id"]]).get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (7, 0, 7)
    assert payload["selection"]["commits"] == 3
    assert [a["name"] for a in payload["authors"]] == ["Bob"]


# --------------------------------------------------------------------------- #
# authors endpoints + manual merging
# --------------------------------------------------------------------------- #
def test_authors_payload_mailmapped(api_env):
    payload = api_env.client.get(f"/api/repos/{api_env.mm_id}/authors").get_json()
    assert payload["mailmapPresent"] is True
    assert payload["mailmapMapped"] == 1
    assert payload["merges"] == []
    by_name = {a["name"]: a for a in payload["authors"]}
    assert set(by_name) == {"Alice", "Bob", "Carol"}
    alice = by_name["Alice"]
    assert alice["commits"] == 4
    assert alice["mailmapped"] is True and alice["merged"] is True
    assert len(alice["aliases"]) == 2               # both raw forms recorded
    assert alice["keys"] == ["Alice <alice@example.com>"]  # one canonical key


def test_manual_merge_and_reset(api_env):
    client = api_env.client
    payload = client.get(f"/api/repos/{api_env.mm_id}/authors").get_json()
    by_name = {a["name"]: a for a in payload["authors"]}
    group = [by_name["Bob"]["keys"][0], by_name["Carol"]["keys"][0]]
    try:
        resp = client.post(f"/api/repos/{api_env.mm_id}/authors/merge",
                           json={"ref": "master", "groups": [group]})
        assert resp.status_code == 200, resp.get_data(as_text=True)
        merged_payload = resp.get_json()
        assert len(merged_payload["merges"]) == 1
        assert set(merged_payload["merges"][0]) == set(group)
        merged_authors = {a["name"]: a for a in merged_payload["authors"]}
        assert set(merged_authors) == {"Alice", "Bob"}
        merged = merged_authors["Bob"]             # Bob wins the representative tie
        assert merged["commits"] == 6 and merged["merged"] is True

        # metrics honour the merged author ids
        payload = _metrics(client, api_env.mm_id, authors=[merged["id"]]).get_json()
        m = payload["metrics"]
        assert (m["added"], m["removed"], m["churn"]) == (14, 1, 15)
        assert payload["selection"]["commits"] == 6
        assert m["modifications"] == 5
    finally:
        # resetting returns to the mailmap-only view
        resp = client.post(f"/api/repos/{api_env.mm_id}/authors/merge",
                           json={"ref": "master", "groups": []})
        assert resp.status_code == 200
    assert len(resp.get_json()["authors"]) == 3
    assert resp.get_json()["merges"] == []


def test_plain_repo_manual_merge(api_env):
    client = api_env.client
    payload = client.get(f"/api/repos/{api_env.plain_id}/authors").get_json()
    by_name = {a["name"]: a for a in payload["authors"]}
    assert set(by_name) == {"Alice", "Alice Alt", "Bob", "Carol"}
    group = [by_name["Alice"]["keys"][0], by_name["Alice Alt"]["keys"][0]]
    try:
        resp = client.post(f"/api/repos/{api_env.plain_id}/authors/merge",
                           json={"ref": "master", "groups": [group]})
        assert resp.status_code == 200
        merged_authors = {a["name"]: a for a in resp.get_json()["authors"]}
        assert set(merged_authors) == {"Alice", "Bob", "Carol"}
        alice = merged_authors["Alice"]
        assert alice["commits"] == 4

        payload = _metrics(client, api_env.plain_id, authors=[alice["id"]]).get_json()
        m = payload["metrics"]
        assert (m["added"], m["removed"], m["churn"]) == (9, 2, 11)
        assert payload["selection"]["commits"] == 4
        assert m["modifications"] == 3
        # ownership is relative to the filtered selection -> 1.0 here
        assert payload["authors"][0]["ownership"] == pytest.approx(1.0)

        # unfiltered: ownership is the share of the whole repository churn
        payload = _metrics(client, api_env.plain_id).get_json()
        row = next(a for a in payload["authors"] if a["name"] == "Alice")
        assert row["churn"] == 11
        assert row["ownership"] == pytest.approx(11 / 26)
    finally:
        client.post(f"/api/repos/{api_env.plain_id}/authors/merge",
                    json={"ref": "master", "groups": []})


def test_merge_validation(api_env):
    client = api_env.client
    resp = client.post(f"/api/repos/{api_env.mm_id}/authors/merge", json={"ref": "master"})
    assert resp.status_code == 400
    assert "groups" in resp.get_json()["error"]
    resp = client.post(f"/api/repos/{api_env.mm_id}/authors/merge",
                       json={"ref": "master", "groups": "nope"})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# commits / paths / overview / extra refs
# --------------------------------------------------------------------------- #
def test_commits_endpoint(api_env):
    client = api_env.client
    page = client.get(f"/api/repos/{api_env.mm_id}/commits",
                      query_string={"limit": 500}).get_json()
    assert page["total"] == 10
    subjects = [c["subject"] for c in page["commits"]]
    assert subjects[0] == "Polish main"            # newest first
    assert "Merge feature" not in subjects         # merge commits excluded
    assert page["commits"][0]["authorName"] == "Alice"

    page = client.get(f"/api/repos/{api_env.mm_id}/commits",
                      query_string={"q": "polish"}).get_json()
    assert page["total"] == 1

    payload = client.get(f"/api/repos/{api_env.mm_id}/authors").get_json()
    carol = next(a for a in payload["authors"] if a["name"] == "Carol")
    page = client.get(f"/api/repos/{api_env.mm_id}/commits",
                      query_string={"authors": str(carol["id"])}).get_json()
    assert page["total"] == 3

    page = client.get(f"/api/repos/{api_env.mm_id}/commits",
                      query_string={"limit": 3, "offset": 2}).get_json()
    assert len(page["commits"]) == 3 and page["offset"] == 2


def test_paths_endpoint(api_env):
    payload = api_env.client.get(f"/api/repos/{api_env.mm_id}/paths",
                                 query_string={"q": "util", "limit": 50}).get_json()
    got = [f["path"] for f in payload["files"]]
    assert got == ["lib/util.c", "src/util.c", "src/util.h", "src/utils.h"]
    assert payload["counts"] == {"files": 10, "dirs": 5}
    payload = api_env.client.get(f"/api/repos/{api_env.mm_id}/paths",
                                 query_string={"q": "src/inner"}).get_json()
    assert payload["dirs"] == [{"path": "src/inner", "type": "dir"}]


def test_overview_endpoint(api_env):
    rows = api_env.client.get("/api/overview").get_json()["repos"]
    by_id = {row["repo"]["id"]: row for row in rows}
    mm = by_id[api_env.mm_id]
    assert mm["totals"] == {"commits": 10, "authors": 3, "files": 10, "dirs": 5,
                            "added": 24, "removed": 3, "churn": 27}
    plain = by_id[api_env.plain_id]
    assert plain["totals"]["commits"] == 10
    assert plain["totals"]["authors"] == 4
    assert plain["totals"]["added"] == 23


def test_prepare_extra_ref(api_env):
    client = api_env.client
    resp = client.post(f"/api/repos/{api_env.mm_id}/prepare", json={"ref": "feature"})
    assert resp.status_code == 200
    assert resp.get_json()["ref"] == "feature"

    deadline = time.time() + 60
    job = None
    while time.time() < deadline:
        job = _repo_json(client, api_env.mm_id).get("refJobs", {}).get("feature")
        if job and job.get("status") in ("ready", "error"):
            break
        time.sleep(0.05)
    assert job and job["status"] == "ready", f"feature ref job: {job}"

    detail = client.get(f"/api/repos/{api_env.mm_id}", query_string={"ref": "feature"}).get_json()["repo"]
    assert detail["ref"] == "feature"
    assert detail["stats"]["commits"] == 3         # c1, c2, c3f

    payload = _metrics(client, api_env.mm_id, ref="feature").get_json()
    m = payload["metrics"]
    assert payload["selection"]["commits"] == 3
    assert (m["added"], m["removed"], m["churn"]) == (18, 0, 18)
    assert m["modifications"] == 3
    assert m["churnRate"] == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
def test_error_responses(api_env):
    client = api_env.client

    resp = _metrics(client, "nope-123")
    assert resp.status_code == 404 and "not found" in resp.get_json()["error"].lower()

    resp = _metrics(client, api_env.mm_id, mode="bogus")
    assert resp.status_code == 400
    resp = _metrics(client, api_env.mm_id, path="no/such.file")
    assert resp.status_code == 404
    resp = _metrics(client, api_env.mm_id, path="src", path_type="bogus")
    assert resp.status_code == 400

    resp = client.get("/api/repos/nope-123/authors")
    assert resp.status_code == 404
    resp = client.get("/api/repos/nope-123/commits")
    assert resp.status_code == 404

    resp = client.post("/api/repos/upload", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    resp = client.post("/api/repos/upload",
                       data={"file": (io.BytesIO(b"not a zip"), "notes.txt")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "zip" in resp.get_json()["error"].lower()

    resp = client.post("/api/repos/clone", json={"url": ""})
    assert resp.status_code == 400
    resp = client.post("/api/repos/clone", json={"url": "ftp://example.com/repo.git"})
    assert resp.status_code == 400
    resp = client.post(f"/api/repos/{api_env.plain_id}/prepare", json={"ref": "no-such-ref"})
    # queued job fails asynchronously -> status accepted but the job reports the error
    assert resp.status_code == 200


def test_prepare_unknown_ref_reports_error(api_env):
    client = api_env.client
    deadline = time.time() + 30
    job = None
    while time.time() < deadline:
        job = _repo_json(client, api_env.plain_id).get("refJobs", {}).get("no-such-ref")
        if job and job.get("status") in ("ready", "error"):
            break
        time.sleep(0.05)
    assert job and job["status"] == "error"


# --------------------------------------------------------------------------- #
# persistence across restarts
# --------------------------------------------------------------------------- #
def test_restart_reattaches_repos(api_env):
    """A fresh app on the same data dir (server restart) still serves metrics.

    Registry entries carry no live git handle; it must be re-attached lazily
    for refs, authors and metric queries to keep working.
    """
    client2 = create_app(DATA_DIR).test_client()
    rows = client2.get("/api/repos").get_json()["repos"]
    assert {r["id"] for r in rows} >= {api_env.mm_id, api_env.plain_id}

    detail = client2.get(f"/api/repos/{api_env.mm_id}").get_json()["repo"]
    assert detail["status"] == "ready"
    assert detail["ref"] == "master"
    assert any(r["name"] == "master" for r in detail["refs"])

    payload = _metrics(client2, api_env.mm_id).get_json()
    m = payload["metrics"]
    assert (m["added"], m["removed"], m["churn"]) == (24, 3, 27)
    authors = client2.get(f"/api/repos/{api_env.mm_id}/authors").get_json()
    assert {a["name"] for a in authors["authors"]} == {"Alice", "Bob", "Carol"}

    payload = _metrics(client2, api_env.plain_id).get_json()
    assert payload["metrics"]["added"] == 23


# --------------------------------------------------------------------------- #
# deletion (kept last: it removes the fixtures created above)
# --------------------------------------------------------------------------- #
def test_delete_flow(api_env):
    client = api_env.client
    for rid in (api_env.mm_id, api_env.plain_id):
        resp = client.delete(f"/api/repos/{rid}")
        assert resp.status_code == 200
        assert resp.get_json() == {"deleted": rid}
        assert client.get(f"/api/repos/{rid}").status_code == 404
        assert client.delete(f"/api/repos/{rid}").status_code == 404
    rows = client.get("/api/overview").get_json()["repos"]
    assert all(row["repo"]["id"] not in (api_env.mm_id, api_env.plain_id) for row in rows)
