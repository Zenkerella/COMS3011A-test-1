"""Deterministic git fixtures and an independent reference implementation.

Three pieces live here:

* :func:`build_script_repo` — builds a repository whose scripted history
  exercises every rule in the brief: additions, modifications, deletions,
  a pure rename, a rename-with-edit, a binary file, a mailmap identity and
  a merge commit that must never contribute metrics.
* :func:`build_tiny_repo` — a minimal history whose middle commit has an
  empty diff, guarding the parser edge case seen in real repositories.
* the ``naive_*`` functions — a deliberately simple reference that re-reads
  history with one subprocess per commit and recomputes all metrics with
  plain python loops.  Nothing here imports :mod:`rat`, so cross-checking
  the production engine against it is a genuine independent verification.

The byte layouts parsed below (``git show --numstat -z``) were verified
against git 2.43: a commit's entries are ``<a>\\t<r>\\t<path>\\0``,
renames are ``<a>\\t<r>\\t\\0<old>\\0<new>\\0`` and binaries are
``-\\t-\\t<path>\\0``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

T0 = 1_600_000_000
STEP = 3600

ALICE = ("Alice", "alice@example.com")
ALICE_ALT = ("Alice Alt", "alice@work.example")
BOB = ("Bob", "bob@example.com")
CAROL = ("Carol", "carol@example.com")

MAILMAP_LINE = "Alice <alice@example.com> <alice@work.example>\n"

ROOT_TXT = "root\n"
MAIN_V1 = "int main() {\n    return 0;\n}\n"                              # 3 lines
MAIN_V2 = "int main() {\n    int x = 1;\n    return x;\n}\n"              # +2 -1
MAIN_V3 = "int main() {\n    int x = 1;\n    return x; /* done */\n}\n"   # +1 -1
UTIL_C_V1 = "int util_a;\nint util_b;\nint util_c;\nint util_d;\n"        # 4 lines
UTIL_C_V2 = UTIL_C_V1 + "int util_e;\n"                                   # +1
UTIL_H = "#ifndef UTIL_H\n#define UTIL_H\n"                               # 2 lines
DEEP_C = "// deep\nint one;\nint two;\nint three;\nint four;\n"           # 5 lines
README_V1 = "# Readme\nSome docs\n"                                       # 2 lines
README_V2 = "# Readme\nSome docs\nMore text\nAnd more\n"                  # +2
LOGO = bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01, 0x02, 0x03, 0xFF, 0x00, 0x10])


# --------------------------------------------------------------------------- #
# Fixture builder
# --------------------------------------------------------------------------- #
def _git(repo: Path, args: list[str], *, author=None, ts=None, check=True) -> bytes:
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": os.devnull,   # isolate from the host's config
        "GIT_CONFIG_SYSTEM": os.devnull,
        "LC_ALL": "C.UTF-8",
    }
    if author is not None:
        name, email = author
        stamp = f"{ts} +0000"
        env.update({
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": stamp,
        })
    proc = subprocess.run(["git", "-C", str(repo), *args], env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout


def build_script_repo(dest: Path, *, mailmap: bool) -> dict[str, str]:
    """Create the scripted repository.  Returns {tag: sha} for every commit
    (including ``"m1"``, the merge commit that metrics must exclude)."""
    if dest.exists():
        shutil.rmtree(dest)              # idempotent: restart from scratch
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, ["init", "-q", "-b", "master"])
    _git(dest, ["config", "commit.gpgsign", "false"])
    _git(dest, ["config", "core.autocrlf", "false"])
    _git(dest, ["config", "gc.auto", "0"])

    commits: dict[str, str] = {}

    def write(rel: str, content):
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, "utf-8")

    def commit(tag: str, author, ts: int, message: str) -> None:
        _git(dest, ["add", "-A"])
        _git(dest, ["commit", "-q", "-m", message], author=author, ts=ts)
        commits[tag] = _git(dest, ["rev-parse", "HEAD"]).decode().strip()

    # c1 - initial import (Alice): root note, main source, docs [, mailmap]
    if mailmap:
        write(".mailmap", MAILMAP_LINE)
    write("root.txt", ROOT_TXT)
    write("src/main.c", MAIN_V1)
    write("docs/readme.md", README_V1)
    commit("c1", ALICE, T0 + 0 * STEP, "Initial import")

    # c2 - more sources (Bob); also the branch point
    write("src/util.c", UTIL_C_V1)
    write("src/util.h", UTIL_H)
    commit("c2", BOB, T0 + 1 * STEP, "Add util")

    # c3f - feature branch (Carol): lives in a nested directory
    _git(dest, ["checkout", "-q", "-b", "feature"])
    write("src/inner/deep.c", DEEP_C)
    commit("c3f", CAROL, T0 + 2 * STEP, "Add deep module")
    _git(dest, ["checkout", "-q", "master"])

    # c4 - edit main.c (Alice Alt, +2 -1)
    write("src/main.c", MAIN_V2)
    commit("c4", ALICE_ALT, T0 + 3 * STEP, "Main work")

    # m1 - merge feature into master; excluded from every metric set
    _git(dest, ["merge", "--no-ff", "-q", "-m", "Merge feature", "feature"],
         author=ALICE, ts=T0 + 4 * STEP)
    commits["m1"] = _git(dest, ["rev-parse", "HEAD"]).decode().strip()

    # c5 - binary asset (Bob): not measured
    write("assets/logo.bin", LOGO)
    commit("c5", BOB, T0 + 5 * STEP, "Add logo")

    # c6 - delete root.txt (Carol): removals only
    _git(dest, ["rm", "-q", "root.txt"])
    commit("c6", CAROL, T0 + 6 * STEP, "Drop root note")

    # c7 - pure rename (Alice): no metric impact at all
    _git(dest, ["mv", "src/util.h", "src/utils.h"])
    commit("c7", ALICE, T0 + 7 * STEP, "Rename util header")

    # c8 - rename with edit (Bob): +1 attributed to the new path
    (dest / "lib").mkdir(exist_ok=True)
    _git(dest, ["mv", "src/util.c", "lib/util.c"])
    write("lib/util.c", UTIL_C_V2)
    commit("c8", BOB, T0 + 8 * STEP, "Move util to lib")

    # c9 - docs update (Carol, +2)
    write("docs/readme.md", README_V2)
    commit("c9", CAROL, T0 + 9 * STEP, "Document more")

    # c10 - tiny edit (Alice Alt, +1 -1)
    write("src/main.c", MAIN_V3)
    commit("c10", ALICE_ALT, T0 + 10 * STEP, "Polish main")

    return commits


def build_tiny_repo(dest: Path) -> dict[str, str]:
    """Minimal history: add a file, one EMPTY commit, edit the file.

    Real repositories (git.git, redis, ...) contain empty-diff commits;
    git emits them with no numstat entries at all (a bare header token in
    the ``-z`` stream), which is exactly the edge case this fixture
    guards.  Returns {tag: sha}."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    _git(dest, ["init", "-q", "-b", "master"])
    _git(dest, ["config", "commit.gpgsign", "false"])
    _git(dest, ["config", "gc.auto", "0"])

    commits: dict[str, str] = {}

    def commit(tag: str, author, ts: int, message: str, allow_empty: bool = False) -> None:
        _git(dest, ["add", "-A"])
        args = ["commit", "-q", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        _git(dest, args, author=author, ts=ts)
        commits[tag] = _git(dest, ["rev-parse", "HEAD"]).decode().strip()

    (dest / "a.txt").write_text("one\n", "utf-8")
    commit("t1", ALICE, T0, "Add a")
    commit("t2", BOB, T0 + STEP, "Empty commit", allow_empty=True)
    (dest / "a.txt").write_text("two\n", "utf-8")
    commit("t3", ALICE, T0 + 2 * STEP, "Edit a")
    return commits


def zip_tree(src: Path, dest_zip: Path, arcname: str) -> Path:
    """Zip a worktree (including ``.git``) exactly like a user upload would."""
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _dirnames, filenames in os.walk(src):
            for fn in filenames:
                fp = Path(dirpath) / fn
                rel = fp.relative_to(src).as_posix()
                zf.write(fp, f"{arcname}/{rel}")
    return dest_zip


# --------------------------------------------------------------------------- #
# Naive reference implementation (no rat imports on purpose)
# --------------------------------------------------------------------------- #
_NAIVE_FLAGS = [
    "-c", "log.showSignature=false",
    "-c", "diff.renames=true",
    "-c", "core.quotePath=false",
]


def raw_git(repo: Path, args: list[str], *, input_bytes: bytes | None = None) -> bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}
    proc = subprocess.run(["git", *_NAIVE_FLAGS, "-C", str(repo), *args], env=env,
                          input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"{proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout


def naive_commits(repo: Path, ref: str = "master") -> list[dict]:
    """All non-merge commits reachable from ref, chronological."""
    out = raw_git(repo, ["log", ref, "--no-merges", "--format=%H%x01%an%x01%ae%x01%ct"])
    rows = []
    for line in out.decode("utf-8", "surrogateescape").splitlines():
        if not line:
            continue
        sha, name, email, ct = line.split("\x01")
        rows.append({"sha": sha, "name": name, "email": email, "ct": int(ct)})
    rows.sort(key=lambda r: r["ct"])
    return rows


def naive_changes(repo: Path, sha: str) -> list[tuple[str, int, int]]:
    """(final path, added, removed) records of one commit, parsed entry by
    entry from a single-commit ``git show`` dump."""
    raw = raw_git(repo, ["show", "--numstat", "-z", "--find-renames=50%",
                         "--format=%H", sha])
    body = raw.split(b"\x00", 1)[1]          # drop "<sha>" prefix
    if body.startswith(b"\n"):
        body = body[1:]
    toks = body.split(b"\x00")
    changes: list[tuple[str, int, int]] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if not tok:
            continue
        parts = tok.split(b"\t", 2)
        if len(parts) != 3:
            continue
        a_raw, r_raw, path = parts
        if a_raw == b"-":                    # binary: not measured
            if path == b"":
                i += 2                       # skip rename operands
            continue
        try:
            added, removed = int(a_raw), int(r_raw)
        except ValueError:
            continue
        if path == b"":
            if i + 1 >= len(toks):
                break
            final = toks[i + 1]              # new path of the rename
            i += 2
        else:
            final = path
        if added or removed:                 # pure renames contribute nothing
            changes.append((final.decode("utf-8", "surrogateescape"), added, removed))
    return changes


def naive_mailmap(repo: Path, identities: list[str]) -> dict[str, str]:
    """raw -> canonical for identities the mailmap rewrites (git's own help)."""
    uniq = sorted(set(identities))
    if not uniq:
        return {}
    payload = ("\n".join(uniq) + "\n").encode("utf-8", "surrogateescape")
    out = raw_git(repo, ["check-mailmap", "--stdin"], input_bytes=payload)
    lines = out.decode("utf-8", "surrogateescape").splitlines()
    return {r: m for r, m in zip(uniq, lines) if r != m}


def naive_history(repo: Path, ref: str = "master", *, mailmap: bool) -> list[dict]:
    """Commits enriched with canonical author, raw identity and records."""
    commits = naive_commits(repo, ref)
    raws = [f'{c["name"]} <{c["email"]}>' for c in commits]
    mm = naive_mailmap(repo, raws) if mailmap else {}
    for c in commits:
        raw = f'{c["name"]} <{c["email"]}>'
        c["raw"] = raw
        c["author"] = mm.get(raw, raw)
        c["records"] = naive_changes(repo, c["sha"])
    return commits


def author_name(canonical: str) -> str:
    return canonical.split(" <", 1)[0]


def ancestors_of(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:k]) for k in range(1, len(parts))]


def in_scope(path: str, scope) -> bool:
    """scope: None (repository), ("file", p) or ("dir", d)."""
    if scope is None:
        return True
    kind, val = scope
    if kind == "file":
        return path == val
    if val == "":
        return True
    return path.startswith(val + "/")


def naive_query(commits: list[dict], s_idx: list[int], scope=None) -> dict:
    """Recompute every metric for the selection in plain python loops.

    Returns ``metrics`` plus per-file / per-dir / per-author dictionaries.
    Record paths are the FINAL paths (renames attribute content to the new
    name only), mirroring the brief.
    """
    added = removed = 0
    files: dict[str, dict] = {}
    dirs: dict[str, dict] = {}
    authors: dict[str, dict] = {}
    mod_commits: set[int] = set()

    for idx in s_idx:
        c = commits[idx]
        arow = authors.setdefault(c["author"], {"commits": 0, "added": 0,
                                                "removed": 0, "mods": set()})
        arow["commits"] += 1
        c_churn = 0
        for (p, a, r) in c["records"]:
            if not in_scope(p, scope):
                continue
            added += a
            removed += r
            c_churn += a + r
            frow = files.setdefault(p, {"added": 0, "removed": 0, "mods": set()})
            frow["added"] += a
            frow["removed"] += r
            frow["mods"].add(idx)
            arow["added"] += a
            arow["removed"] += r
            arow["mods"].add(idx)
            for d in ancestors_of(p):
                drow = dirs.setdefault(d, {"added": 0, "removed": 0, "mods": set()})
                drow["added"] += a
                drow["removed"] += r
                drow["mods"].add(idx)
        if c_churn > 0:
            mod_commits.add(idx)

    n = len(s_idx)
    churn = added + removed
    return {
        "selectionSize": n,
        "metrics": {
            "added": added,
            "removed": removed,
            "growth": added - removed,
            "churn": churn,
            "modifications": len(mod_commits),
            "modificationFrequency": (len(mod_commits) / n) if n else 0.0,
            "churnRate": (churn / n) if n else 0.0,
        },
        "files": files,
        "dirs": dirs,
        "authors": authors,
    }


def naive_select(commits: list[dict], *, mode: str = "all", from_ts=None, to_ts=None,
                 shas=None, author_names=None) -> list[int]:
    """Chronological indices of the commits satisfying the query filters."""
    sel = list(range(len(commits)))
    if mode == "range":
        sel = [i for i in sel
               if (from_ts is None or commits[i]["ct"] >= from_ts)
               and (to_ts is None or commits[i]["ct"] < to_ts)]
    elif mode == "list":
        wanted = set(shas or [])
        sel = [i for i in sel if commits[i]["sha"] in wanted]
    if author_names:
        wanted = set(author_names)
        sel = [i for i in sel if author_name(commits[i]["author"]) in wanted]
    return sel
