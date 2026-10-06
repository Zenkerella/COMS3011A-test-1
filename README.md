# RAT — Repository Analysis Tool

A web dashboard that measures software-development metrics from git history,
built for the COMS3011A test brief. It ingests repositories from an uploaded
**zip archive** (a working tree including `.git`) or by **cloning a URL**,
parses the full non-merge history once, and answers metric queries about any
**commit set** and any **file / directory / repository** scope — including
per-author numbers — in milliseconds, even for repositories with tens of
thousands of commits.

```
┌───────────────────────────── dashboard ─────────────────────────────┐
│  repo picker · commit-set (all | range | commit list) · authors ·   │
│  file/directory scope · metric cards · charts · files/dirs/authors  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Quick start

Requirements: **Python 3.10+**, **git** on `PATH`, Flask + numpy
(see `requirements.txt`).

```bash
pip install -r requirements.txt
python3 server.py                 # serves http://127.0.0.1:8000/
```

Open the dashboard, then add a repository:

* **Upload** — zip a work tree *including its `.git` directory* and upload it
  (e.g. `zip -r repo.zip . -x '.git/objects/pack/tmp*'`), or
* **Clone** — paste any clone URL (https, ssh, or a local path).

`python3 server.py --port 9000 --data /tmp/rat-data` customises the port and
the data directory (default: `./data`).

## Metrics and semantics (per the brief)

| Metric | Definition |
| --- | --- |
| Added / Removed | lines added / removed by the selected commits on the scoped object |
| Growth | `added − removed` |
| Churn | `added + removed` |
| Modifications | number of selected commits with churn > 0 |
| Modification frequency | `modifications / |S|` (|S| = size of the commit set) |
| Churn rate | `churn / |S|` |
| Author ownership | author churn / total churn of the selection |

* **Commit sets** — all commits reachable from a reference, a time range
  `[from, to)` on **committer** dates, or an explicit list of commit hashes.
* **Scopes** — repository, any directory (aggregating all descendants), or a
  single file.
* **Renames** are detected at the 50 % similarity threshold; a rename moves
  the object’s *content* delta to the new path, so a pure rename contributes
  nothing (history renames never inflate churn).
* **Deletions** are recorded as line removals on the path they vanished from.
* **Binary files** are listed but never measured.
* **Merge commits** are excluded from every metric.
* **Authors** are merged via the repository’s `.mailmap` (resolved with
  `git check-mailmap`) and can additionally be merged/split manually in the
  UI; ownership and per-author rows honour both.
* **Multiple repositories** can be loaded at once and switched between at any
  time.

## Architecture

```
server.py            Flask entry point (CLI: --host --port --data)
rat/
  config.py          paths, cache version, upload guards
  gitrepo.py         thin git CLI wrapper (log -z / ls-tree / check-mailmap / resolve)
  ingest.py          zip extraction (safe) + clone, then background ingest job
  numstat.py         one-pass parser of `git log --numstat -z` → numpy arrays,
                     path/dir indexes, ancestor CSR table, pickle cache
  authors.py         mailmap clustering + manual merge groups → author ids
  metrics.py         vectorised query engine (commit mask → record mask →
                     sums / distinct-commit counts / per-author / per-dir)
  store.py           repository registry, background jobs, cache lifecycle
  api.py             JSON API (see below)
web/                 dependency-free frontend (vanilla JS, inline SVG charts)
tests/               pytest suite + deterministic fixture generator + an
                     independent "naive" reference implementation
```

Data flow: `git log --numstat -z` (one process per ref) → byte-level token
parse → compact numpy record arrays + indices → pickled cache (versioned by
`CACHE_VERSION` and the resolved commit sha) → every query is a few array
operations, no git subprocesses.

### HTTP API (short form)

```
GET    /api/config                     product metadata
GET    /api/repos                      list tracked repositories
POST   /api/repos/clone {url,name}     start a clone job
POST   /api/repos/upload  (multipart)  start a zip ingest job
GET    /api/repos/<id>[?ref=]          status, stats, ref jobs
DELETE /api/repos/<id>                 remove repository (work tree + cache)
GET    /api/overview                   totals per repository
POST   /api/repos/<id>/prepare {ref}   parse an additional ref in background
GET    /api/repos/<id>/authors         merged author identities
POST   /api/repos/<id>/authors/merge   set manual merge groups {ref,groups}
GET    /api/repos/<id>/commits         paged, searchable commit list
GET    /api/repos/<id>/paths?q=        file/directory autocomplete
POST   /api/repos/<id>/metrics         the query engine (see request fields:
       ref, commits{mode,from,to,shas}, authors[], path, pathType, buckets)
```

## Performance

Measured on this machine against real repositories (full, exact agreement
with independent git ground truth — see *Validation*):

| repo | commits (non-merge) | git log | RAT parse+index (once) | cache load | repo-wide query |
| --- | --- | --- | --- | --- | --- |
| cJSON | 955 | 0.2 s | 0.2 s | < 0.05 s | ~2 ms |
| redis | 11 875 | ~9 s | ~9 s | < 0.05 s | ~10 ms |
| git.git | 61 101 | 26.5 s | 27 s | 0.01 s | ~28 ms |

Ingestion is dominated by git itself (diff + rename detection): the RAT
parser processes the 12 MB dump at ~85 MB/s (0.14 s); building indices and the
cache adds the rest. Caches make restarts instant, and directory/author/file
queries stay in the tens of milliseconds on ~135 000 change records.

## Testing

```bash
python3 -m pytest tests -q          # 90 tests, seconds to run
```

The suite is deliberately adversarial:

* **Deterministic fixtures** — a scripted 10-commit history (add/modify/
  delete, pure rename, rename-with-edit, binary file, `.mailmap`, branch +
  merge) plus a tiny history containing an empty-diff commit; a second copy
  without a mailmap.
* **Token-level parse tests** — byte layouts of `git log/show --numstat -z`
  (merged header/first entry, renames, binaries, empty commits).
* **Hand-computed metric expectations** protect the reference; **an
  independent naive implementation** (`tests/helpers.py`, no `rat` imports,
  one `git show` per commit, plain loops) cross-checks the engine across 17
  scenarios × 2 repositories (commit modes, scopes, author filters).
* **End-to-end API tests** ingest a zip and a clone through Flask, then
  exercise every endpoint including merges, extra refs, error paths and
  delete.

### Validation on real repositories

`tests` prove internal consistency; correctness against **real** history was
verified separately on `DaveGamble/cJSON`, `redis/redis` and `git/git` by
comparing the engine with two independent git invocations (plain
`git log --numstat` totals; `--author=` anchored to an exact identity).
Result: **every number matches exactly** — commit counts, added, removed,
growth, churn, modifications and per-author totals; e.g. git.git 4 070 371 /
2 375 604 lines added/removed over 61 101 commits. This exercise surfaced and
fixed a real parser bug (empty-diff commits arrive as bare header tokens and
were dropped), now guarded by regression tests.

## Notes

* Uploaded zips are extracted defensively (path traversal, symlinks, entry
  and size limits per `rat/config.py`).
* The registry, work trees and caches live under `data/`; parses are cached
  per resolved commit sha, so pulling a branch invalidates only that ref.
