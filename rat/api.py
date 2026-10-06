"""HTTP API for the RAT dashboard."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from . import __version__
from .config import MAX_UPLOAD_BYTES
from .gitrepo import GitError
from .ingest import IngestError
from .metrics import QueryRequest
from .store import READY, Repo, RepoStore

api = Blueprint("api", __name__, url_prefix="/api")

ALLOWED_URL_PREFIXES = ("http://", "https://", "ssh://", "git://", "file://", "/", "./", "../", "~")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def _store() -> RepoStore:
    return current_app.extensions["store"]


def _repo_or_404(rid: str) -> Repo:
    repo = _store().get(rid)
    if repo is None:
        raise ApiError(f"Repository {rid!r} not found.", 404)
    return repo


def _body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def _ts(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError as exc:
        raise ApiError(f"Invalid timestamp: {value!r}") from exc


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


# --------------------------------------------------------------------------- #
# meta
# --------------------------------------------------------------------------- #
@api.get("/config")
def get_config():
    return jsonify({
        "version": __version__,
        "dataDir": str(_store().data_dir),
        "maxUploadBytes": MAX_UPLOAD_BYTES,
        "features": ["zip", "clone", "multi-repo", "filters", "author-merge",
                     "mailmap", "metrics"],
    })


@api.get("/repos")
def list_repos():
    return jsonify({"repos": _store().list()})


@api.post("/repos/clone")
def create_clone():
    body = _body()
    url = str(body.get("url") or "").strip()
    name = str(body.get("name") or "").strip() or None
    if not url:
        raise ApiError("A repository URL is required.")
    if len(url) > 500:
        raise ApiError("Repository URL is too long.")
    if url.startswith("-") or not url.startswith(ALLOWED_URL_PREFIXES):
        raise ApiError(
            "Unsupported URL. Use an https://, http://, ssh://, git://, file:// URL "
            "or a local path."
        )
    repo = _store().add_clone(url, name)
    return jsonify({"repo": repo.public()}), 201


@api.post("/repos/upload")
def create_upload():
    file = request.files.get("file")
    if file is None or not file.filename:
        raise ApiError("No file received. Attach a zip archive of the repository.")
    if not file.filename.lower().endswith(".zip"):
        raise ApiError("Only .zip archives are supported.")
    name = (request.form.get("name") or Path(file.filename).stem).strip()
    tmp_dir = _store().data_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4().hex}.zip"
    file.save(tmp_path)
    if tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        raise ApiError("Uploaded archive is empty.")
    repo = _store().add_zip(tmp_path, name)
    return jsonify({"repo": repo.public()}), 201


@api.get("/repos/<rid>")
def repo_detail(rid: str):
    repo = _repo_or_404(rid)
    ref = request.args.get("ref") or None
    detail = _store().repo_detail(repo, ref)
    return jsonify({"repo": detail})


@api.delete("/repos/<rid>")
def delete_repo(rid: str):
    if not _store().delete(rid):
        raise ApiError(f"Repository {rid!r} not found.", 404)
    return jsonify({"deleted": rid})


@api.get("/overview")
def overview():
    store = _store()
    rows = []
    for meta in store.list():
        row = {"repo": {k: v for k, v in meta.items() if k != "merges"}}
        if meta.get("status") == READY:
            repo = store.get(meta["id"])
            if repo is not None:
                try:
                    ref = repo.meta.get("defaultRef") or "HEAD"
                    rc = store.ensure_ref(repo, ref)
                    data = rc.data
                    if rc.author_state is None and repo.git is not None:
                        rc.ensure_authors(repo.git, ref)
                    row["totals"] = {
                        "commits": data.n_commits,
                        "authors": len(rc.author_state.authors) if rc.author_state else len(set(data.raw_ids)),
                        "files": data.n_paths,
                        "dirs": max(0, data.n_dirs - 1),
                        "added": int(data.rec_added.sum()) if data.n_records else 0,
                        "removed": int(data.rec_removed.sum()) if data.n_records else 0,
                    }
                    row["totals"]["churn"] = row["totals"]["added"] + row["totals"]["removed"]
                except Exception as exc:
                    row["totalsError"] = str(exc)
        rows.append(row)
    return jsonify({"repos": rows})


# --------------------------------------------------------------------------- #
# refs / preparation
# --------------------------------------------------------------------------- #
@api.post("/repos/<rid>/prepare")
def prepare_ref(rid: str):
    repo = _repo_or_404(rid)
    body = _body()
    ref = str(body.get("ref") or repo.meta.get("defaultRef") or "HEAD")
    if repo.status != READY and repo.status != "parsing":
        raise ApiError(f"Repository is not ready (status: {repo.status}).", 409)
    try:
        job = _store().prepare_ref_async(repo, ref)
    except GitError as exc:
        raise ApiError(str(exc), 400) from exc
    return jsonify({"ref": ref, "job": job})


# --------------------------------------------------------------------------- #
# authors
# --------------------------------------------------------------------------- #
def _authors_payload(repo: Repo, ref: str) -> dict:
    store = _store()
    try:
        rc, state = store.authors_for(repo, ref)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc
    return {
        "ref": ref,
        "mailmapPresent": rc.data.mailmap_present,
        "mailmapMapped": len(rc.mailmap_map),
        "merges": rc.merges,
        "authors": state.authors,
    }


@api.get("/repos/<rid>/authors")
def get_authors(rid: str):
    repo = _repo_or_404(rid)
    ref = request.args.get("ref") or repo.meta.get("defaultRef") or "HEAD"
    return jsonify(_authors_payload(repo, ref))


@api.post("/repos/<rid>/authors/merge")
def merge_authors(rid: str):
    repo = _repo_or_404(rid)
    body = _body()
    ref = str(body.get("ref") or repo.meta.get("defaultRef") or "HEAD")
    groups = body.get("groups")
    if groups is None:
        raise ApiError("Missing 'groups' (list of author-key groups to merge).")
    if not isinstance(groups, list):
        raise ApiError("'groups' must be a list of lists of author keys.")
    try:
        _store().set_merges(repo, ref, groups)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc
    return jsonify(_authors_payload(repo, ref))


# --------------------------------------------------------------------------- #
# commits / paths
# --------------------------------------------------------------------------- #
@api.get("/repos/<rid>/commits")
def list_commits(rid: str):
    repo = _repo_or_404(rid)
    ref = request.args.get("ref") or repo.meta.get("defaultRef") or "HEAD"
    offset = _clamp(request.args.get("offset"), 0, 10_000_000, 0)
    limit = _clamp(request.args.get("limit"), 1, 500, 50)
    q = request.args.get("q") or ""
    authors_raw = request.args.get("authors") or ""
    author_ids = [int(x) for x in authors_raw.split(",") if x.strip().isdigit()] if authors_raw else []
    from_ts = _ts(request.args.get("from"))
    to_ts = _ts(request.args.get("to"))
    try:
        page = _store().commits_page(repo, ref, offset, limit, q, author_ids, from_ts, to_ts)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc
    page["ref"] = ref
    return jsonify(page)


@api.get("/repos/<rid>/paths")
def list_paths(rid: str):
    repo = _repo_or_404(rid)
    ref = request.args.get("ref") or repo.meta.get("defaultRef") or "HEAD"
    q = request.args.get("q") or ""
    limit = _clamp(request.args.get("limit"), 1, 500, 60)
    try:
        payload = _store().paths_lookup(repo, ref, q, limit)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc
    payload["ref"] = ref
    return jsonify(payload)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
@api.post("/repos/<rid>/metrics")
def compute_metrics(rid: str):
    repo = _repo_or_404(rid)
    body = _body()
    ref = str(body.get("ref") or repo.meta.get("defaultRef") or "HEAD")

    commits = body.get("commits") or {}
    if not isinstance(commits, dict):
        raise ApiError("'commits' must be an object.")
    mode = str(commits.get("mode") or "all").lower()
    if mode not in ("all", "range", "list"):
        raise ApiError("commits.mode must be one of: all, range, list.")

    shas = commits.get("shas") or []
    if not isinstance(shas, list):
        raise ApiError("commits.shas must be a list of commit hashes.")
    shas = [str(s) for s in shas if isinstance(s, str) and 0 < len(s) <= 80][:20000]

    author_ids = body.get("authors") or []
    if not isinstance(author_ids, list):
        raise ApiError("'authors' must be a list of author ids.")
    author_ids = [int(a) for a in author_ids if isinstance(a, (int, float)) or (isinstance(a, str) and a.isdigit())]

    path = body.get("path")
    path_type = body.get("pathType")
    if path is not None:
        path = str(path)
        if len(path) > 1000:
            raise ApiError("Path is too long.")
    if path_type is not None and path_type not in ("file", "dir"):
        raise ApiError("pathType must be 'file' or 'dir'.")

    # resolve the reference (and ensure its cache exists)
    try:
        rc = _store().ensure_ref(repo, ref)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc

    # infer/validate the path against the parsed repository
    if path is not None:
        data = rc.data
        pid = data.path_id(path)
        did = data.dir_id(path)
        if path_type == "file" and pid is None:
            raise ApiError(f"File {path!r} not found in this repository/reference.", 404)
        if path_type == "dir" and did is None:
            raise ApiError(f"Directory {path!r} not found in this repository/reference.", 404)
        if path_type is None:
            if pid is not None:
                path_type = "file"
            elif did is not None:
                path_type = "dir"
            else:
                raise ApiError(f"{path!r} is not a known file or directory.", 404)
        if path_type == "dir" and did is None:
            raise ApiError(f"Directory {path!r} not found in this repository/reference.", 404)

    req = QueryRequest(
        ref=ref,
        commit_mode=mode,
        from_ts=_ts(commits.get("from")),
        to_ts=_ts(commits.get("to")),
        shas=shas,
        author_ids=author_ids,
        path=path,
        path_type=path_type,
        top=_clamp(body.get("top"), 1, 2000, 150),
        buckets=_clamp(body.get("buckets"), 16, 3000, 320),
    )
    started = time.time()
    try:
        result = _store().query(repo, ref, req)
    except (GitError, IngestError, ValueError) as exc:
        raise ApiError(str(exc), 400) from exc
    result["elapsedMs"] = round((time.time() - started) * 1000, 1)
    return jsonify(result)
