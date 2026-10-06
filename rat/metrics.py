"""Metric computation engine.

Every metric in the brief maps onto vectorised array operations over the
records produced by :mod:`rat.numstat`:

======================  ====================================================
Metric                  Implementation
======================  ====================================================
Added / Removed lines   sums of per-(commit,path) deltas restricted to the
                        commit set S and the object scope
Growth                  added - removed
Churn                   added + removed
Modifications           #commits with churn(h, o) > 0 (distinct commits)
Modification frequency  modifications / |S|      (0 when |S| = 0)
Churn rate              churn / |S|              (0 when |S| = 0)
Author modifications    #commits authored by a with churn(h, o) > 0
Author churn            churn(h, o) restricted to commits authored by a
Author ownership        author churn / total churn (0 when total churn = 0)
======================  ====================================================

Directory / repository metrics aggregate their descendant files, which is
exactly the recursive expansion of the brief's "immediate child" sums.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from .authors import AuthorState
from .numstat import RepoData


# --------------------------------------------------------------------------- #
# Request model
# --------------------------------------------------------------------------- #
@dataclass
class QueryRequest:
    ref: str = "HEAD"
    commit_mode: str = "all"            # all | range | list
    from_ts: int | None = None          # inclusive
    to_ts: int | None = None            # exclusive
    shas: list[str] = field(default_factory=list)
    author_ids: list[int] = field(default_factory=list)   # empty = all authors
    path: str | None = None
    path_type: str | None = None        # file | dir | None (repository scope)
    top: int = 150
    buckets: int = 320

    def cache_key(self, author_version: int) -> tuple:
        return (
            self.commit_mode, self.from_ts, self.to_ts,
            tuple(sorted(self.shas)) if self.commit_mode == "list" else None,
            tuple(sorted(self.author_ids)),
            self.path, self.path_type, self.top, self.buckets,
            author_version,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def build_commit_mask(data: RepoData, authors: AuthorState, req: QueryRequest) -> np.ndarray:
    n = data.n_commits
    if n == 0:
        return np.zeros(0, dtype=np.bool_)

    if req.commit_mode == "range":
        mask = np.ones(n, dtype=np.bool_)
        lo = req.from_ts if req.from_ts is not None else -(1 << 62)
        mask &= data.times >= lo
        if req.to_ts is not None:
            mask &= data.times < req.to_ts
    elif req.commit_mode == "list":
        mask = np.zeros(n, dtype=np.bool_)
        if req.shas:
            sha_idx = data.sha_index
            idx = np.fromiter(
                (i for i in (sha_idx.get(s) for s in req.shas) if i is not None),
                dtype=np.int64,
            )
            if idx.size:
                mask[idx] = True
    else:  # all
        mask = np.ones(n, dtype=np.bool_)

    if req.author_ids:
        amask = np.zeros(len(authors.authors), dtype=np.bool_)
        valid = [i for i in req.author_ids if 0 <= i < len(authors.authors)]
        if valid:
            amask[valid] = True
        mask &= amask[authors.author_ids]
    return mask


def _dir_aggregate(data: RepoData, path_values: np.ndarray) -> np.ndarray:
    """Aggregate per-path values up the directory tree (root inclusive)."""
    npaths = data.n_paths
    ndirs = data.n_dirs
    out = np.zeros(ndirs, dtype=np.float64)
    if npaths == 0:
        return out
    reps = (data.anc_ptr[1:] - data.anc_ptr[:-1]).astype(np.int64)
    total = int(reps.sum())
    if total == 0:
        return out
    starts = data.anc_ptr[:-1].astype(np.int64)
    offsets = np.repeat(starts, reps)
    seg_start = np.repeat(np.cumsum(reps) - reps, reps)
    anc_idx = offsets + (np.arange(total, dtype=np.int64) - seg_start)
    pid_expanded = np.repeat(np.arange(npaths, dtype=np.int64), reps)
    weights = path_values[pid_expanded]
    return np.bincount(data.anc_ids[anc_idx], weights=weights, minlength=ndirs)


def _dir_modifications(data: RepoData, rec_commit: np.ndarray, rec_path: np.ndarray,
                       mask: np.ndarray) -> np.ndarray:
    """#distinct commits with churn>0 on each directory."""
    ndirs = data.n_dirs
    npaths = data.n_paths
    out = np.zeros(ndirs, dtype=np.int64)
    if not mask.any() or npaths == 0:
        return out
    keys = rec_commit[mask].astype(np.int64) * npaths + rec_path[mask].astype(np.int64)
    ukeys = np.unique(keys)
    u_commit = ukeys // npaths
    u_path = (ukeys % npaths).astype(np.int64)

    reps = (data.anc_ptr[1:] - data.anc_ptr[:-1]).astype(np.int64)[u_path]
    total = int(reps.sum())
    if total == 0:
        return out
    starts = data.anc_ptr[:-1].astype(np.int64)[u_path]
    offsets = np.repeat(starts, reps)
    seg_start = np.repeat(np.cumsum(reps) - reps, reps)
    anc_idx = offsets + (np.arange(total, dtype=np.int64) - seg_start)
    dir_ids = data.anc_ids[anc_idx].astype(np.int64)
    commits_u = np.repeat(u_commit, reps)
    pairs = np.unique(commits_u * ndirs + dir_ids)
    return np.bincount(pairs % ndirs, minlength=ndirs).astype(np.int64)


def _distinct_commit_count(rec_commit: np.ndarray, mask: np.ndarray, ncommits: int) -> int:
    if not mask.any():
        return 0
    binned = np.bincount(rec_commit[mask].astype(np.int64), minlength=ncommits)
    return int(np.count_nonzero(binned))


def _iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Main query
# --------------------------------------------------------------------------- #
def run_query(data: RepoData, authors: AuthorState, req: QueryRequest) -> dict:
    n = data.n_commits
    npaths = data.n_paths
    ndirs = data.n_dirs

    mask = build_commit_mask(data, authors, req)
    s_idx = np.flatnonzero(mask)
    nsel = int(s_idx.size)

    # ----- object scope ---------------------------------------------------
    scope_desc = {"path": req.path, "type": req.path_type or "repo"}
    if req.path_type == "file" and req.path is not None:
        pid = data.path_id(req.path)
        if pid is None:
            scope_rec = np.zeros(data.n_records, dtype=np.bool_)
            win_lo = win_hi = 0
        else:
            scope_rec = data.rec_path == pid
            win_lo, win_hi = pid, pid + 1
    elif req.path_type == "dir" and req.path is not None:
        win_lo, win_hi = data.path_range(req.path)
        scope_rec = ((data.rec_path >= win_lo) & (data.rec_path < win_hi)) if npaths else np.zeros(0, dtype=np.bool_)
    else:
        win_lo, win_hi = 0, npaths
        scope_rec = None

    # ----- record selection ----------------------------------------------
    if data.n_records == 0 or nsel == 0:
        rec_mask = np.zeros(data.n_records, dtype=np.bool_)
    elif scope_rec is None:
        rec_mask = mask[data.rec_commit]
    else:
        rec_mask = mask[data.rec_commit] & scope_rec

    added = int(data.rec_added[rec_mask].sum()) if data.n_records else 0
    removed = int(data.rec_removed[rec_mask].sum()) if data.n_records else 0
    churn = added + removed
    growth = added - removed
    mod_mask = rec_mask & (data.rec_churn > 0)
    modifications = _distinct_commit_count(data.rec_commit, mod_mask, n)
    mod_freq = modifications / nsel if nsel else 0.0
    churn_rate = churn / nsel if nsel else 0.0

    # ----- per-commit series ---------------------------------------------
    series = _build_series(data, rec_mask, s_idx, req.buckets)

    # With a file/dir scope, only authors with real activity on the object
    # are listed; at repository scope every author of the selection appears.
    object_scoped = (
        (req.path_type == "file" and req.path is not None)
        or (req.path_type == "dir" and req.path not in (None, ""))
    )

    # ----- author metrics for the scope ----------------------------------
    author_rows = _author_rows(data, authors, rec_mask, mod_mask, mask, s_idx, churn,
                               object_scoped=object_scoped)

    # ----- files table ----------------------------------------------------
    files_rows, file_stats = _files_table(data, rec_mask, mod_mask, win_lo, win_hi, nsel, req.top)

    # ----- directories table ---------------------------------------------
    dirs_rows, dir_stats = _dirs_table(data, rec_mask, mod_mask, req, nsel)

    first_sha = data.shas[int(s_idx[0])] if nsel else None
    last_sha = data.shas[int(s_idx[-1])] if nsel else None

    return {
        "object": scope_desc,
        "selection": {
            "ref": req.ref,
            "mode": req.commit_mode,
            "commits": nsel,
            "totalCommits": n,
            "authorFilter": len(req.author_ids),
            "from": _iso(req.from_ts),
            "to": _iso(req.to_ts),
            "firstCommit": first_sha and {
                "sha": first_sha, "date": _iso(int(data.times[int(s_idx[0])])),
                "subject": data.subjects[int(s_idx[0])],
            },
            "lastCommit": last_sha and {
                "sha": last_sha, "date": _iso(int(data.times[int(s_idx[-1])])),
                "subject": data.subjects[int(s_idx[-1])],
            },
        },
        "metrics": {
            "added": added,
            "removed": removed,
            "growth": growth,
            "churn": churn,
            "modifications": modifications,
            "modificationFrequency": mod_freq,
            "churnRate": churn_rate,
        },
        "series": series,
        "authors": author_rows,
        "files": files_rows,
        "directories": dirs_rows,
        "tables": {"files": file_stats, "directories": dir_stats},
    }


def _build_series(data: RepoData, rec_mask: np.ndarray, s_idx: np.ndarray, buckets: int = 320) -> list[dict]:
    nsel = int(s_idx.size)
    if nsel == 0 or data.n_records == 0:
        return []
    nb = max(1, min(buckets, nsel))
    bounds = np.linspace(0, nsel, nb + 1).astype(np.int64)

    added_per_commit = np.bincount(
        data.rec_commit[rec_mask].astype(np.int64),
        weights=data.rec_added[rec_mask].astype(np.float64),
        minlength=data.n_commits,
    )
    removed_per_commit = np.bincount(
        data.rec_commit[rec_mask].astype(np.int64),
        weights=data.rec_removed[rec_mask].astype(np.float64),
        minlength=data.n_commits,
    )
    added_c = added_per_commit[s_idx]
    removed_c = removed_per_commit[s_idx]
    starts = bounds[:-1]

    a_chunks = np.add.reduceat(added_c, starts)
    r_chunks = np.add.reduceat(removed_c, starts)

    out: list[dict] = []
    cumulative = 0
    for i in range(nb):
        end = int(bounds[i + 1]) - 1
        a = int(a_chunks[i])
        r = int(r_chunks[i])
        cumulative += a - r
        out.append({
            "t": int(data.times[int(s_idx[end])]),
            "added": a,
            "removed": r,
            "growth": a - r,
            "cumulativeGrowth": cumulative,
            "commits": int(bounds[i + 1] - bounds[i]),
        })
    return out


def _author_rows(data: RepoData, authors: AuthorState, rec_mask: np.ndarray,
                 mod_mask: np.ndarray, mask: np.ndarray, s_idx: np.ndarray,
                 total_churn: int, object_scoped: bool = False) -> list[dict]:
    na = len(authors.authors)
    if na == 0:
        return []
    rec_author = authors.author_ids[data.rec_commit]  # per record

    commits_by_a = np.bincount(authors.author_ids[s_idx], minlength=na) if s_idx.size else np.zeros(na, dtype=np.int64)
    churn_by_a = np.bincount(rec_author[rec_mask], weights=data.rec_churn[rec_mask].astype(np.float64),
                             minlength=na) if rec_mask.any() else np.zeros(na)
    added_by_a = np.bincount(rec_author[rec_mask], weights=data.rec_added[rec_mask].astype(np.float64),
                             minlength=na) if rec_mask.any() else np.zeros(na)
    removed_by_a = np.bincount(rec_author[rec_mask], weights=data.rec_removed[rec_mask].astype(np.float64),
                               minlength=na) if rec_mask.any() else np.zeros(na)

    mods_by_a = np.zeros(na, dtype=np.int64)
    if mod_mask.any():
        distinct_commits = np.unique(data.rec_commit[mod_mask])
        mods_by_a = np.bincount(authors.author_ids[distinct_commits], minlength=na)

    rows = []
    for aid in range(na):
        c_churn = float(churn_by_a[aid])
        a_commits = int(commits_by_a[aid])
        if a_commits == 0 and c_churn == 0:
            continue
        if object_scoped and c_churn == 0:
            continue  # nothing was contributed to this file/directory
        info = authors.authors[aid]
        rows.append({
            "id": aid,
            "name": info["name"],
            "email": info["email"],
            "commits": a_commits,
            "added": int(added_by_a[aid]),
            "removed": int(removed_by_a[aid]),
            "growth": int(added_by_a[aid]) - int(removed_by_a[aid]),
            "churn": int(c_churn),
            "modifications": int(mods_by_a[aid]),
            "ownership": (c_churn / total_churn) if total_churn > 0 else 0.0,
            "aliases": info["aliases"],
            "mailmapped": info["mailmapped"],
            "merged": info["merged"],
            "keys": info["keys"],
        })
    rows.sort(key=lambda r: (-r["churn"], -r["commits"], r["name"]))
    return rows


def _files_table(data: RepoData, rec_mask: np.ndarray, mod_mask: np.ndarray,
                 win_lo: int, win_hi: int, nsel: int, top: int):
    npaths = data.n_paths
    if npaths == 0 or win_lo >= win_hi:
        return [], {"inScope": 0, "withActivity": 0, "shown": 0}
    if nsel == 0:
        return [], {"inScope": win_hi - win_lo, "withActivity": 0, "shown": 0}

    added = np.bincount(data.rec_path[rec_mask], weights=data.rec_added[rec_mask].astype(np.float64),
                        minlength=npaths)
    removed = np.bincount(data.rec_path[rec_mask], weights=data.rec_removed[rec_mask].astype(np.float64),
                          minlength=npaths)
    mods = np.zeros(npaths, dtype=np.int64)
    if mod_mask.any():
        keys = data.rec_commit[mod_mask].astype(np.int64) * npaths + data.rec_path[mod_mask].astype(np.int64)
        ukeys = np.unique(keys)
        mods = np.bincount(ukeys % npaths, minlength=npaths)

    churn = added + removed
    window = slice(win_lo, win_hi)
    w_added = added[window]
    w_removed = removed[window]
    w_mods = mods[window]
    w_churn = churn[window]

    has_records = data.path_has_records  # cached bool array
    w_has_records = has_records[window]

    cand = np.flatnonzero((w_churn > 0) | w_has_records)
    with_activity = int(np.count_nonzero(w_churn > 0))

    order = sorted(cand.tolist(), key=lambda i: (-float(w_churn[i]), data.paths[win_lo + i]))
    limit = max(1, top)
    chosen = order[:limit]

    rows = []
    for i in chosen:
        pid = win_lo + i
        p = data.paths[pid]
        rows.append({
            "path": p,
            "depth": p.count("/"),
            "added": int(w_added[i]),
            "removed": int(w_removed[i]),
            "growth": int(w_added[i]) - int(w_removed[i]),
            "churn": int(w_churn[i]),
            "modifications": int(w_mods[i]),
            "modificationFrequency": (float(w_mods[i]) / nsel) if nsel else 0.0,
            "churnRate": (float(w_churn[i]) / nsel) if nsel else 0.0,
            "atTip": bool(data.path_at_tip[pid]),
            "binary": bool(data.path_binary[pid]),
        })
    return rows, {
        "inScope": int(len(cand)),
        "withActivity": with_activity,
        "shown": len(rows),
        "total": win_hi - win_lo,
    }


def _dirs_table(data: RepoData, rec_mask: np.ndarray, mod_mask: np.ndarray,
                req: QueryRequest, nsel: int, top_dirs: int = 400):
    ndirs = data.n_dirs
    if ndirs <= 1 and req.path is None:
        return [], {"inScope": 0, "withActivity": 0, "shown": 0}

    if req.path_type == "dir" and req.path is not None:
        lo, hi = data.subdir_range(req.path)
        if req.path == "":
            lo, hi = 1, ndirs
    elif req.path_type == "file":
        lo = hi = 0
    else:
        lo, hi = 1, ndirs  # exclude root: repository metrics are the summary cards

    path_added = np.bincount(data.rec_path[rec_mask], weights=data.rec_added[rec_mask].astype(np.float64),
                             minlength=data.n_paths) if data.n_records else np.zeros(data.n_paths)
    path_removed = np.bincount(data.rec_path[rec_mask], weights=data.rec_removed[rec_mask].astype(np.float64),
                               minlength=data.n_paths) if data.n_records else np.zeros(data.n_paths)

    dir_added = _dir_aggregate(data, path_added)
    dir_removed = _dir_aggregate(data, path_removed)
    dir_mods = _dir_modifications(data, data.rec_commit, data.rec_path, mod_mask)

    dir_churn = dir_added + dir_removed
    if lo >= hi:
        return [], {"inScope": 0, "withActivity": 0, "shown": 0}
    if nsel == 0:
        return [], {"inScope": hi - lo, "withActivity": 0, "shown": 0}

    w_churn = dir_churn[lo:hi]
    cand = np.flatnonzero(w_churn > 0)
    with_activity = int(cand.size)
    order = sorted(cand.tolist(), key=lambda i: (-float(w_churn[i]), data.dirs[lo + i]))
    chosen = order[:max(1, top_dirs)]

    rows = []
    for i in chosen:
        did = lo + i
        d = data.dirs[did]
        rows.append({
            "path": d,
            "depth": int(data.dir_depth[did]),
            "added": int(dir_added[did]),
            "removed": int(dir_removed[did]),
            "growth": int(dir_added[did]) - int(dir_removed[did]),
            "churn": int(dir_churn[did]),
            "modifications": int(dir_mods[did]),
            "modificationFrequency": (float(dir_mods[did]) / nsel) if nsel else 0.0,
            "churnRate": (float(dir_churn[did]) / nsel) if nsel else 0.0,
        })
    return rows, {
        "inScope": hi - lo,
        "withActivity": with_activity,
        "shown": len(rows),
        "total": ndirs - 1,
    }
