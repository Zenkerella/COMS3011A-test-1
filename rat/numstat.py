"""Parse ``git log --numstat -z`` output into compact numpy arrays.

The parsed representation is the substrate every metric is computed from:

* ``commits_*`` arrays: one entry per non-merge commit reachable from the
  chosen reference, sorted chronologically (oldest first).
* ``rec_*`` arrays: one entry per (commit, path) change with the added and
  removed line counts from that commit to its predecessor.  Renames (>=50%
  similarity) contribute only their *content* delta, attributed to the new
  path, so a pure rename yields no record at all.  Deleted objects appear as
  their line removals on the path they vanished from.  Binary files are never
  recorded ("binary files are not measured").
* Path/directory indexes, including a CSR-style ancestor table so that any
  directory metric is a vectorised prefix-aggregation of file metrics.

Directory metrics are defined over immediate children; because that sum is
recursive, aggregating every descendant file is exactly equivalent.
"""
from __future__ import annotations

import json
import pickle
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import CACHE_VERSION
from .gitrepo import GitRepo, _decode

_CACHE_PREFIX = "cache"


@dataclass
class RepoData:
    """Immutable, cacheable parse of a repository at one reference."""

    ref: str
    ref_sha: str
    generated_at: float
    mailmap_present: bool

    # commits (chronological order)
    shas: list[str]
    raw_ids: list[str]           # "Name <email>" as recorded by git (pre-mailmap)
    times: np.ndarray            # int64 committer timestamps
    subjects: list[str]

    # change records
    rec_commit: np.ndarray       # int32 index into commits
    rec_path: np.ndarray         # int32 index into paths
    rec_added: np.ndarray        # int32
    rec_removed: np.ndarray      # int32
    rec_churn: np.ndarray        # int32 = added + removed

    # path index (sorted lexicographically)
    paths: list[str]
    path_binary: np.ndarray      # bool: seen as binary in some diff
    path_at_tip: np.ndarray      # bool: exists in the tree at ref
    path_has_records: np.ndarray  # bool: appears in at least one change record

    # directory index (sorted; "" is the root)
    dirs: list[str]
    dir_parent: np.ndarray       # int32 parent dir id (-1 for root)
    dir_depth: np.ndarray        # int32

    # ancestor CSR: for path i, ancestor dirs are anc_ids[anc_ptr[i]:anc_ptr[i+1]]
    anc_ptr: np.ndarray          # int32 [npaths+1]
    anc_ids: np.ndarray          # int32

    # lazily built lookup tables (not part of the persisted cache contract)
    _sha_index_map: dict | None = field(default=None, repr=False, compare=False)

    # ---- convenience -----------------------------------------------------
    @property
    def sha_index(self) -> dict[str, int]:
        """sha -> chronological commit index (built on first use)."""
        if self._sha_index_map is None or len(self._sha_index_map) != self.n_commits:
            self._sha_index_map = {s: i for i, s in enumerate(self.shas)}
        return self._sha_index_map
    @property
    def n_commits(self) -> int:
        return len(self.shas)

    @property
    def n_paths(self) -> int:
        return len(self.paths)

    @property
    def n_records(self) -> int:
        return int(self.rec_commit.size)

    @property
    def n_dirs(self) -> int:
        return len(self.dirs)

    def path_id(self, path: str) -> int | None:
        i = bisect_left(self.paths, path)
        if i < len(self.paths) and self.paths[i] == path:
            return i
        return None

    def dir_id(self, path: str) -> int | None:
        i = bisect_left(self.dirs, path)
        if i < len(self.dirs) and self.dirs[i] == path:
            return i
        return None

    def path_range(self, directory: str) -> tuple[int, int]:
        """[lo, hi) range of path ids strictly inside ``directory`` (any depth)."""
        if directory == "":
            return 0, len(self.paths)
        prefix = directory + "/"
        lo = bisect_left(self.paths, prefix)
        hi = bisect_left(self.paths, prefix + "\U0010ffff")
        return lo, hi

    def subdir_range(self, directory: str) -> tuple[int, int]:
        """[lo, hi) range of dir ids strictly inside ``directory``."""
        if directory == "":
            return 1, len(self.dirs)  # skip root itself
        prefix = directory + "/"
        lo = bisect_right(self.dirs, directory)
        hi = bisect_left(self.dirs, prefix + "\U0010ffff")
        return lo, hi

    def summary(self) -> dict:
        first_t = int(self.times[0]) if self.n_commits else 0
        last_t = int(self.times[-1]) if self.n_commits else 0
        tip_files = int(self.path_at_tip.sum()) if self.n_paths else 0
        return {
            "ref": self.ref,
            "refSha": self.ref_sha,
            "generatedAt": self.generated_at,
            "mailmapPresent": self.mailmap_present,
            "commits": self.n_commits,
            "authors": len(set(self.raw_ids)),
            "paths": self.n_paths,
            "dirs": max(0, self.n_dirs - 1),
            "tipFiles": tip_files,
            "records": self.n_records,
            "binaryFiles": int(self.path_binary.sum()) if self.n_paths else 0,
            "firstCommitTime": first_t,
            "lastCommitTime": last_t,
        }


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_HEADER = b"C\x01"


def _split_stats(buf: bytes) -> tuple[int | None, int | None, bytes]:
    t1 = buf.find(b"\t")
    if t1 == -1:
        return None, None, b""
    t2 = buf.find(b"\t", t1 + 1)
    if t2 == -1:
        return None, None, b""
    a_raw, r_raw = buf[:t1], buf[t1 + 1:t2]
    path = buf[t2 + 1:]
    try:
        added = int(a_raw) if a_raw != b"-" else None
        removed = int(r_raw) if r_raw != b"-" else None
    except ValueError:
        return None, None, path
    return added, removed, path


def parse_numstat_bytes(raw: bytes, progress=None) -> dict:
    """Tokenise the raw dump. Returns dict of python-side columns."""
    commit_sha: list[bytes] = []
    commit_name: list[bytes] = []
    commit_email: list[bytes] = []
    commit_ct: list[int] = []
    commit_subject: list[bytes] = []

    rec_commit: list[int] = []
    rec_path: list[bytes] = []
    rec_added: list[int] = []
    rec_removed: list[int] = []

    seen_paths: set[bytes] = set()
    binary_paths: set[bytes] = set()

    tokens = raw.split(b"\x00")
    total_tokens = len(tokens) or 1
    commit_idx = -1
    pending_rename: tuple[int | None, int | None] | None = None
    rename_old: bytes | None = None

    for i, tok in enumerate(tokens):
        if progress is not None and (i & 0xFFFF) == 0:
            progress(15 + 40.0 * i / total_tokens, "Parsing history")
        if not tok:
            pending_rename = None
            rename_old = None
            continue

        if pending_rename is not None:
            if rename_old is None:
                rename_old = tok
                seen_paths.add(tok)
                continue
            # new path of the rename
            added, removed = pending_rename
            new_path = tok
            seen_paths.add(new_path)
            if added is None or removed is None:
                binary_paths.add(new_path)
                binary_paths.add(rename_old)
            elif added or removed:
                rec_commit.append(commit_idx)
                rec_path.append(new_path)
                rec_added.append(added)
                rec_removed.append(removed)
            pending_rename = None
            rename_old = None
            continue

        if tok.startswith(_HEADER):
            nl = tok.find(b"\n")
            if nl == -1:
                # Empty-diff commit: git emits the bare header token with no
                # trailing newline and no numstat entries.  The commit still
                # belongs to the history and must be counted (it simply has
                # zero modifications).
                header, rest = tok, b""
            else:
                header, rest = tok[:nl], tok[nl + 1:]
            parts = header[2:].split(b"\x01")
            if len(parts) < 5:
                continue
            subject = b"\x01".join(parts[4:])
            commit_sha.append(parts[0])
            commit_name.append(parts[1])
            commit_email.append(parts[2])
            try:
                commit_ct.append(int(parts[3]))
            except ValueError:
                commit_ct.append(0)
            commit_subject.append(subject)
            commit_idx += 1
            if rest:
                added, removed, path = _split_stats(rest)
                if not path and path is not None:
                    pending_rename = (added, removed)
                elif path:
                    seen_paths.add(path)
                    if added is None or removed is None:
                        binary_paths.add(path)
                    elif added or removed:
                        rec_commit.append(commit_idx)
                        rec_path.append(path)
                        rec_added.append(added)
                        rec_removed.append(removed)
            continue

        # plain entry token
        added, removed, path = _split_stats(tok)
        if not path:
            pending_rename = (added, removed)
            continue
        seen_paths.add(path)
        if added is None or removed is None:
            binary_paths.add(path)
        elif added or removed:
            rec_commit.append(commit_idx)
            rec_path.append(path)
            rec_added.append(added)
            rec_removed.append(removed)

    return {
        "commit_sha": commit_sha,
        "commit_name": commit_name,
        "commit_email": commit_email,
        "commit_ct": commit_ct,
        "commit_subject": commit_subject,
        "rec_commit": rec_commit,
        "rec_path": rec_path,
        "rec_added": rec_added,
        "rec_removed": rec_removed,
        "seen_paths": seen_paths,
        "binary_paths": binary_paths,
    }


def build_repo_data(gitrepo: GitRepo, ref: str, parsed: dict, tip_paths: list[str],
                    mailmap_present: bool, progress=None) -> RepoData:
    n = len(parsed["commit_sha"])
    if progress:
        progress(60, "Building indices")

    # ---- commits, chronological order ----
    times = np.asarray(parsed["commit_ct"], dtype=np.int64)
    order = np.argsort(times, kind="stable")
    remap = np.empty(n, dtype=np.int64)
    remap[order] = np.arange(n, dtype=np.int64)

    shas = [_decode(parsed["commit_sha"][i]) for i in order]
    names = [_decode(parsed["commit_name"][i]) for i in order]
    emails = [_decode(parsed["commit_email"][i]) for i in order]
    subjects = [_decode(parsed["commit_subject"][i]) for i in order]
    raw_ids = [f"{nm} <{em}>" for nm, em in zip(names, emails)]

    # ---- records ----
    rec_commit = np.asarray(parsed["rec_commit"], dtype=np.int64)
    if rec_commit.size:
        rec_commit = remap[rec_commit].astype(np.int32)
    else:
        rec_commit = np.zeros(0, dtype=np.int32)
    rec_added = np.asarray(parsed["rec_added"], dtype=np.int32)
    rec_removed = np.asarray(parsed["rec_removed"], dtype=np.int32)
    rec_churn = rec_added + rec_removed

    # ---- path index ----
    seen = {_decode(p) for p in parsed["seen_paths"]}
    seen.update(tip_paths)
    paths = sorted(seen)
    pid = {p: i for i, p in enumerate(paths)}
    npaths = len(paths)

    rec_path_str = [_decode(p) for p in parsed["rec_path"]]
    rec_path = np.fromiter((pid[p] for p in rec_path_str), dtype=np.int32,
                           count=len(rec_path_str)) if rec_path_str else np.zeros(0, dtype=np.int32)

    binary = np.zeros(npaths, dtype=np.bool_)
    for p in parsed["binary_paths"]:
        i = pid.get(_decode(p))
        if i is not None:
            binary[i] = True
    at_tip = np.zeros(npaths, dtype=np.bool_)
    for p in tip_paths:
        i = pid.get(p)
        if i is not None:
            at_tip[i] = True
    has_records = np.zeros(npaths, dtype=np.bool_)
    if rec_path.size:
        has_records[np.unique(rec_path)] = True

    # ---- directory index + ancestor CSR ----
    dir_set = {""}
    for p in paths:
        parts = p.split("/")
        for k in range(1, len(parts)):
            dir_set.add("/".join(parts[:k]))
    dirs = sorted(dir_set)
    did = {d: i for i, d in enumerate(dirs)}
    ndirs = len(dirs)

    dir_parent = np.full(ndirs, -1, dtype=np.int32)
    dir_depth = np.zeros(ndirs, dtype=np.int32)
    for d, i in did.items():
        if d:
            parent = d.rsplit("/", 1)[0] if "/" in d else ""
            dir_parent[i] = did[parent]
            dir_depth[i] = d.count("/") + 1

    # Each path's ancestor dirs, root first.
    anc_flat = np.empty(0, dtype=np.int32)
    anc_ptr = np.zeros(npaths + 1, dtype=np.int64)
    if npaths:
        ids: list[int] = []
        ptr = np.zeros(npaths + 1, dtype=np.int64)
        for i, p in enumerate(paths):
            parts = p.split("/")
            chain = [0]  # root
            acc = ""
            for k in range(1, len(parts)):
                acc = parts[k - 1] if not acc else acc + "/" + parts[k - 1]
                chain.append(did[acc])
            ptr[i + 1] = ptr[i] + len(chain)
            ids.extend(chain)
        anc_flat = np.asarray(ids, dtype=np.int32)
        anc_ptr = ptr

    if progress:
        progress(75, "Serialising cache")

    return RepoData(
        ref=ref,
        ref_sha="",
        generated_at=time.time(),
        mailmap_present=mailmap_present,
        shas=shas,
        raw_ids=raw_ids,
        times=times[order] if n else np.zeros(0, dtype=np.int64),
        subjects=subjects,
        rec_commit=rec_commit,
        rec_path=rec_path,
        rec_added=rec_added,
        rec_removed=rec_removed,
        rec_churn=rec_churn,
        paths=paths,
        path_binary=binary,
        path_at_tip=at_tip,
        path_has_records=has_records,
        dirs=dirs,
        dir_parent=dir_parent,
        dir_depth=dir_depth,
        anc_ptr=anc_ptr.astype(np.int64),
        anc_ids=anc_flat,
    )


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def cache_path(cache_dir: Path, ref_sha: str) -> Path:
    return cache_dir / f"{_CACHE_PREFIX}-v{CACHE_VERSION}-{ref_sha}.pkl"


def meta_path(cache_dir: Path, ref_sha: str) -> Path:
    return cache_dir / f"{_CACHE_PREFIX}-v{CACHE_VERSION}-{ref_sha}.meta.json"


def save_cache(data: RepoData, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_path(cache_dir, data.ref_sha).with_suffix(".pkl.tmp")
    with open(tmp, "wb") as fh:
        pickle.dump(data, fh, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(cache_path(cache_dir, data.ref_sha))
    with open(meta_path(cache_dir, data.ref_sha), "w", encoding="utf-8") as fh:
        json.dump(data.summary(), fh)


def load_cache(cache_dir: Path, ref_sha: str) -> RepoData | None:
    p = cache_path(cache_dir, ref_sha)
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as fh:
            data = pickle.load(fh)
        if not isinstance(data, RepoData) or data.ref_sha != ref_sha:
            return None
        return data
    except Exception:
        return None


def build_and_cache(gitrepo: GitRepo, ref: str, cache_dir: Path, progress=None) -> RepoData:
    """Full pipeline: run git log, parse, build indices, persist cache."""
    ref_sha = gitrepo.resolve_commit(ref)
    if ref_sha is None:
        raise ValueError(f"reference {ref!r} does not resolve to a commit")
    if progress:
        progress(5, "Reading commit history (git log --numstat)")
    raw = gitrepo.raw_numstat_log(ref)
    if progress:
        progress(15, f"Parsing history ({len(raw) / 1e6:.0f} MB)")
    parsed = parse_numstat_bytes(raw, progress=progress)
    del raw
    tip_paths = gitrepo.ls_tree_paths(ref_sha)
    data = build_repo_data(gitrepo, ref, parsed, tip_paths,
                           gitrepo.has_mailmap(ref_sha), progress=progress)
    data.ref_sha = ref_sha
    if progress:
        progress(80, "Writing parse cache")
    save_cache(data, cache_dir)
    if progress:
        progress(100, "Ready")
    return data
