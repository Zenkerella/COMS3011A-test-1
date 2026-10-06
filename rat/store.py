"""Repository registry and lifecycle management.

Each ingested repository lives under ``data/repos/<id>/``:

* ``repo.git/`` - bare mirror clone (remote URL ingestion)
* ``src/``      - extracted tree (zip ingestion)
* ``cache/``    - parsed-history pickles (one per reference)

The registry itself is a JSON file rewritten atomically.  Long-running work
(clone, extract, parse) happens on background threads and reports progress
through the repo's meta record.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np

from . import metrics as metrics_mod
from .authors import (AuthorState, build_author_state, load_mailmap_map,
                      sanitize_merge_groups)
from .config import MAX_HOT_REPOS, QUERY_CACHE_SIZE
from .gitrepo import GitError, GitRepo
from .ingest import IngestError, clone_repo, extract_zip, locate_git_repo
from .numstat import RepoData, build_and_cache, load_cache

ACTIVE_STATUSES = {"queued", "cloning", "extracting", "parsing", "loading"}
READY = "ready"
ERROR = "error"


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return (s or "repo")[:48]


class RefCache:
    """Everything the metric engine needs for one (repo, ref) pair."""

    def __init__(self, data: RepoData):
        self.data = data
        self.mailmap_map: dict[str, str] = {}
        self.merges: list[list[str]] = []
        self.author_state: AuthorState | None = None
        self.author_version = 0
        self.query_cache: OrderedDict = OrderedDict()
        self.lock = threading.RLock()

    def ensure_authors(self, gitrepo: GitRepo, ref: str) -> AuthorState:
        with self.lock:
            if self.author_state is None:
                self.mailmap_map = load_mailmap_map(gitrepo, ref, self.data.raw_ids)
                self.author_state = build_author_state(
                    self.data.raw_ids, self.mailmap_map, self.merges,
                    version=self.author_version,
                )
            return self.author_state

    def invalidate_authors(self, merges: list[list[str]]) -> None:
        with self.lock:
            self.merges = merges
            self.author_version += 1
            self.author_state = None
            self.query_cache.clear()

    def memo_query(self, key, computer):
        with self.lock:
            if key in self.query_cache:
                value = self.query_cache.pop(key)
                self.query_cache[key] = value
                return value
        value = computer()
        with self.lock:
            self.query_cache[key] = value
            while len(self.query_cache) > QUERY_CACHE_SIZE:
                self.query_cache.popitem(last=False)
        return value


class Repo:
    def __init__(self, meta: dict, repos_dir: Path):
        self.meta = meta
        self.repos_dir = repos_dir
        self.lock = threading.RLock()
        self.git: GitRepo | None = None
        self.refs: dict[str, RefCache] = {}
        self.deleted = False
        self.last_used = time.time()

    # ------------------------------------------------------------------ meta
    @property
    def id(self) -> str:
        return self.meta["id"]

    @property
    def root(self) -> Path:
        return self.repos_dir / self.id

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def status(self) -> str:
        return self.meta.get("status", "queued")

    def set_status(self, status: str, message: str = "", progress: float | None = None,
                   error: str | None = None) -> None:
        self.meta["status"] = status
        if message:
            self.meta["message"] = message
        if progress is not None:
            self.meta["progress"] = round(max(0.0, min(100.0, progress)), 1)
        if error is not None:
            self.meta["error"] = error
        self.meta["updatedAt"] = time.time()

    def public(self) -> dict:
        m = dict(self.meta)
        m.pop("merges", None)
        m["loadedRefs"] = sorted(self.refs.keys())
        return m


class RepoStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repos_dir = self.data_dir / "repos"
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._repos: dict[str, Repo] = {}
        self._registry_path = self.data_dir / "registry.json"
        self._load_registry()

    # -------------------------------------------------------------- registry
    def _load_registry(self) -> None:
        if not self._registry_path.is_file():
            return
        try:
            raw = json.loads(self._registry_path.read_text("utf-8"))
        except Exception:
            raw = {"repos": []}
        for meta in raw.get("repos", []):
            if not isinstance(meta, dict) or "id" not in meta:
                continue
            if meta.get("status") in ACTIVE_STATUSES:
                # ingestion was interrupted by a restart
                meta["status"] = ERROR
                meta["error"] = "Interrupted by a server restart. Remove and re-add the repository."
            self._repos[meta["id"]] = Repo(meta, self.repos_dir)
        self._write_registry()

    def _write_registry(self) -> None:
        payload = {"repos": [r.meta for r in self._repos.values()]}
        tmp = self._registry_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), "utf-8")
        os.replace(tmp, self._registry_path)

    def persist(self) -> None:
        with self._lock:
            self._write_registry()

    # ------------------------------------------------------------------ CRUD
    def list(self) -> list[dict]:
        with self._lock:
            repos = sorted(self._repos.values(), key=lambda r: r.meta.get("createdAt", 0))
            return [r.public() for r in repos]

    def get(self, repo_id: str) -> Repo | None:
        with self._lock:
            repo = self._repos.get(repo_id)
        if repo and not repo.deleted:
            repo.last_used = time.time()
            self._attach_git(repo)
        return repo

    def _attach_git(self, repo: Repo) -> None:
        """Re-attach the git handle of a previously ingested repository.

        Handles do not survive a server restart, so a ready repository
        loaded from the registry is re-opened lazily on first use.
        """
        if repo.git is not None or repo.status != READY:
            return
        with repo.lock:
            if repo.git is not None:
                return
            root = repo.root
            sub = root / ("src" if repo.meta.get("sourceType") == "zip" else "repo.git")
            if sub.is_dir():
                repo.git = locate_git_repo(sub)

    def _new_repo(self, name: str, source_type: str, source: str) -> Repo:
        with self._lock:
            base = _slug(name)
            rid = base
            while rid in self._repos:
                rid = f"{base}-{uuid.uuid4().hex[:6]}"
            meta = {
                "id": rid,
                "name": name or base,
                "sourceType": source_type,
                "source": source,
                "status": "queued",
                "progress": 0.0,
                "message": "Queued",
                "error": None,
                "createdAt": time.time(),
                "updatedAt": time.time(),
                "stats": None,
                "merges": {},
                "defaultRef": None,
            }
            repo = Repo(meta, self.repos_dir)
            self._repos[rid] = repo
            self._write_registry()
            return repo

    def add_clone(self, url: str, name: str | None = None) -> Repo:
        guess = name or url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"
        repo = self._new_repo(guess, "clone", url)
        threading.Thread(target=self._run_clone, args=(repo, url), daemon=True).start()
        return repo

    def add_zip(self, zip_path: Path, name: str | None = None) -> Repo:
        repo = self._new_repo(name or zip_path.stem, "zip", zip_path.name)
        threading.Thread(target=self._run_zip, args=(repo, zip_path, name), daemon=True).start()
        return repo

    def delete(self, repo_id: str) -> bool:
        with self._lock:
            repo = self._repos.pop(repo_id, None)
        if not repo:
            return False
        repo.deleted = True
        repo.refs.clear()
        self._write_registry()
        shutil.rmtree(repo.root, ignore_errors=True)
        return True

    # ------------------------------------------------------------ ingestion
    def _progress_reporter(self, repo: Repo, status: str):
        state = {"last": 0.0}

        def report(pct: float, message: str) -> None:
            if repo.deleted:
                return
            repo.set_status(status, message=message, progress=pct)
            now = time.time()
            if now - state["last"] > 0.75:
                state["last"] = now
                self.persist()

        return report

    def _run_clone(self, repo: Repo, url: str) -> None:
        try:
            repo.set_status("cloning", message=f"Cloning {url}", progress=0)
            self.persist()
            dest = repo.root / "repo.git"
            git = clone_repo(url, dest, self._progress_reporter(repo, "cloning"))
            repo.git = git
            self._finalise_ingest(repo)
        except (IngestError, GitError) as exc:
            self._fail(repo, str(exc))
        except Exception as exc:  # pragma: no cover
            self._fail(repo, f"Unexpected error while cloning: {exc}")

    def _run_zip(self, repo: Repo, zip_path: Path, name: str | None) -> None:
        try:
            repo.set_status("extracting", message="Extracting archive", progress=0)
            self.persist()
            dest = repo.root / "src"
            git = extract_zip(zip_path, dest, self._progress_reporter(repo, "extracting"))
            repo.git = git
            self._finalise_ingest(repo)
        except (IngestError, GitError) as exc:
            self._fail(repo, str(exc))
        except Exception as exc:  # pragma: no cover
            self._fail(repo, f"Unexpected error while extracting: {exc}")
        finally:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _finalise_ingest(self, repo: Repo) -> None:
        git = repo.git
        assert git is not None
        if not git.is_valid():
            self._fail(repo, "The extracted folder is not a valid git repository.")
            return
        default_ref = git.default_ref()
        repo.meta["defaultRef"] = default_ref
        head = git.head_sha(default_ref)
        repo.meta["headSha"] = head
        if head is None:
            repo.meta["stats"] = {"commits": 0}
            repo.set_status(READY, message="Repository is empty (no commits)", progress=100)
            self.persist()
            return
        # Pre-parse the default branch so the first dashboard view is instant.
        try:
            repo.set_status("parsing", message="Reading commit history", progress=0)
            self.persist()
            ref_cache = self.ensure_ref(repo, default_ref, announce=True)
            repo.meta["stats"] = ref_cache.data.summary()
            repo.set_status(READY, message="Ready", progress=100)
            self.persist()
        except Exception as exc:
            self._fail(repo, f"Failed to parse history: {exc}")

    def _fail(self, repo: Repo, message: str) -> None:
        repo.set_status(ERROR, message=message, error=message)
        self.persist()

    # ------------------------------------------------------------- ref cache
    def ensure_ref(self, repo: Repo, ref: str, announce: bool = False,
                   job: dict | None = None) -> RefCache:
        """Return (building if necessary) the parsed cache for a reference.

        ``announce`` mirrors progress onto the repository-level status field
        (used during initial ingestion); ``job`` receives progress for
        on-demand parsing of extra refs.
        """
        with repo.lock:
            cached = repo.refs.get(ref)
            if cached is not None:
                if job is not None:
                    job.update(status="ready", progress=100, message="Ready")
                return cached
            if repo.git is None:
                raise IngestError("Repository is not ready yet.")
            ref_sha = repo.git.resolve_commit(ref)
            if ref_sha is None:
                raise GitError(f"reference {ref!r} does not resolve to a commit")
            existing = load_cache(repo.cache_dir, ref_sha)
            if existing is not None:
                rc = RefCache(existing)
                self._evict_if_needed()
                repo.refs[ref] = rc
                if repo.meta.get("defaultRef") == ref:
                    repo.meta["stats"] = existing.summary()
                if announce:
                    repo.set_status(READY, message="Ready", progress=100)
                if job is not None:
                    job.update(status="ready", progress=100, message="Ready")
                return rc

            state = {"last": 0.0}

            def progress(pct: float, message: str) -> None:
                if announce:
                    repo.set_status("parsing", message=message, progress=pct)
                if job is not None:
                    job.update(status="parsing", progress=round(pct, 1), message=message)
                now = time.time()
                if now - state["last"] > 0.75:
                    state["last"] = now
                    self.persist()

            data = build_and_cache(repo.git, ref, repo.cache_dir, progress=progress)
            rc = RefCache(data)
            self._evict_if_needed()
            repo.refs[ref] = rc
            if repo.meta.get("defaultRef") == ref or not repo.meta.get("defaultRef"):
                repo.meta["stats"] = data.summary()
            if announce:
                repo.set_status(READY, message="Ready", progress=100)
            if job is not None:
                job.update(status="ready", progress=100, message="Ready")
            self.persist()
            return rc

    def prepare_ref_async(self, repo: Repo, ref: str) -> dict:
        """Kick off parsing of (repo, ref) in the background if needed."""
        with repo.lock:
            jobs = repo.meta.setdefault("refJobs", {})
            if ref in repo.refs:
                jobs[ref] = {"status": "ready", "progress": 100, "message": "Ready"}
                return jobs[ref]
            job = jobs.get(ref)
            if job and job.get("status") in ("queued", "parsing"):
                return job
            jobs[ref] = {"status": "queued", "progress": 0, "message": "Queued"}
            threading.Thread(target=self._run_prepare, args=(repo, ref), daemon=True).start()
            return jobs[ref]

    def _run_prepare(self, repo: Repo, ref: str) -> None:
        job = repo.meta.setdefault("refJobs", {}).setdefault(
            ref, {"status": "queued", "progress": 0, "message": "Queued"})
        try:
            self.ensure_ref(repo, ref, job=job)
        except Exception as exc:
            job.update(status="error", error=str(exc), message=str(exc))
            self.persist()

    def _evict_if_needed(self) -> None:
        with self._lock:
            loaded = [(r.last_used, r) for r in self._repos.values() if r.refs]
            while len(loaded) > MAX_HOT_REPOS:
                loaded.sort(key=lambda t: t[0])
                _, victim = loaded.pop(0)
                victim.refs.clear()

    def authors_for(self, repo: Repo, ref: str) -> tuple[RefCache, AuthorState]:
        rc = self.ensure_ref(repo, ref)
        assert repo.git is not None
        state = rc.ensure_authors(repo.git, ref)
        return rc, state

    def known_author_keys(self, rc: RefCache) -> set[str]:
        if rc.author_state is None:
            return set()
        keys: set[str] = set()
        for a in rc.author_state.authors:
            keys.update(a["keys"])
        return keys

    def set_merges(self, repo: Repo, ref: str, groups) -> AuthorState:
        rc, state = self.authors_for(repo, ref)
        known = self.known_author_keys(rc)
        clean = sanitize_merge_groups(groups, known)
        merges = repo.meta.setdefault("merges", {})
        if clean:
            merges[ref] = clean
        else:
            merges.pop(ref, None)
        rc.invalidate_authors(clean)
        assert repo.git is not None
        state = rc.ensure_authors(repo.git, ref)
        self.persist()
        return state

    # ---------------------------------------------------------------- query
    def query(self, repo: Repo, ref: str, req: metrics_mod.QueryRequest) -> dict:
        rc, state = self.authors_for(repo, ref)
        key = req.cache_key(rc.author_version)

        def compute() -> dict:
            result = metrics_mod.run_query(rc.data, state, req)
            result["repo"] = {"id": repo.id, "name": repo.meta.get("name")}
            return result

        return rc.memo_query(key, compute)

    # ------------------------------------------------------------- metadata
    def repo_detail(self, repo: Repo, ref: str | None = None) -> dict:
        detail = repo.public()
        git = repo.git
        if git is None:
            return detail
        try:
            detail["refs"] = git.list_refs()
        except GitError:
            detail["refs"] = []
        ref = ref or repo.meta.get("defaultRef") or "HEAD"
        try:
            rc = self.ensure_ref(repo, ref)
            detail["stats"] = rc.data.summary()
            detail["ref"] = ref
            if rc.author_state is None and repo.git is not None:
                rc.ensure_authors(repo.git, ref)
            if rc.author_state is not None:
                detail["stats"]["authors"] = len(rc.author_state.authors)
        except (GitError, IngestError, ValueError) as exc:
            detail["statsError"] = str(exc)
        detail["refJobs"] = repo.meta.get("refJobs", {})
        return detail

    def commits_page(self, repo: Repo, ref: str, offset: int, limit: int,
                     q: str | None, author_ids: list[int] | None,
                     from_ts: int | None, to_ts: int | None) -> dict:
        rc, state = self.authors_for(repo, ref)
        data = rc.data
        n = data.n_commits

        mask = np.ones(n, dtype=np.bool_)
        if from_ts is not None:
            mask &= data.times >= from_ts
        if to_ts is not None:
            mask &= data.times < to_ts
        if author_ids:
            amask = np.zeros(len(state.authors), dtype=np.bool_)
            valid = [i for i in author_ids if 0 <= i < len(state.authors)]
            if valid:
                amask[valid] = True
            mask &= amask[state.author_ids]
        ql = (q or "").strip().lower()
        if ql:
            idxs = [
                i for i in range(n)
                if ql in data.shas[i].lower()
                or ql in data.subjects[i].lower()
                or ql in data.raw_ids[i].lower()
            ]
            qmask = np.zeros(n, dtype=np.bool_)
            if idxs:
                qmask[idxs] = True
            mask &= qmask

        sel = np.flatnonzero(mask)
        total = int(sel.size)
        # newest first
        sel = sel[::-1]
        page = sel[offset: offset + max(1, min(limit, 500))]
        rows = []
        for i in page:
            i = int(i)
            aid = int(state.author_ids[i])
            a = state.authors[aid]
            rows.append({
                "sha": data.shas[i],
                "time": int(data.times[i]),
                "subject": data.subjects[i],
                "authorId": aid,
                "authorName": a["name"],
                "authorEmail": a["email"],
            })
        return {"total": total, "offset": offset, "limit": limit, "commits": rows}

    def paths_lookup(self, repo: Repo, ref: str, q: str, limit: int) -> dict:
        rc = self.ensure_ref(repo, ref)
        data = rc.data
        ql = (q or "").strip().lower()
        lim = max(1, min(limit, 500))

        def take(items, extra):
            out = []
            for it in items:
                if ql and ql not in it.lower():
                    continue
                out.append(extra(it))
                if len(out) >= lim:
                    break
            return out

        files = take(data.paths, lambda p: {"path": p, "type": "file"})
        dirs = take((d for d in data.dirs if d), lambda d: {"path": d, "type": "dir"})
        return {
            "files": files,
            "dirs": dirs,
            "counts": {"files": data.n_paths, "dirs": max(0, data.n_dirs - 1)},
        }
