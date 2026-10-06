"""Thin, safe wrappers around the git CLI.

Every git invocation goes through :class:`GitRepo` so that ownership checks
(``safe.directory``), rename detection, and raw path handling are configured
consistently.  Paths are read with ``-z`` (NUL separated) so that quoting and
unicode escaping never interfere with metric computation.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_BIN = os.environ.get("RAT_GIT_BIN", "git")

# Consistent configuration for every call.
COMMON_FLAGS = [
    "-c", "safe.directory=*",
    "-c", "core.quotePath=false",
    "-c", "diff.renames=true",
    "-c", "diff.renameLimit=16000",
    "-c", "core.pager=cat",
    "-c", "color.ui=false",
    "-c", "advice.detachedHead=false",
    "-c", "log.showSignature=false",
]

GIT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",   # never block on credential prompts
    "GIT_OPTIONAL_LOCKS": "0",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
}


class GitError(RuntimeError):
    """Raised when a git command fails."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _decode(raw: bytes) -> str:
    """Decode git output preserving otherwise-undecodable bytes."""
    return raw.decode("utf-8", "surrogateescape")


@dataclass
class GitRepo:
    """A git repository reachable either through its working tree or --git-dir."""

    gitdir: Path
    worktree: Path | None = None
    _is_bare: bool | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ core
    @property
    def base_cmd(self) -> list[str]:
        if self.worktree is not None:
            return [GIT_BIN, *COMMON_FLAGS, "-C", str(self.worktree)]
        return [GIT_BIN, *COMMON_FLAGS, "--git-dir", str(self.gitdir)]

    def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [*self.base_cmd, *args]
        try:
            proc = subprocess.run(
                cmd,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=GIT_ENV,
                timeout=timeout,
            )
        except FileNotFoundError as exc:  # git missing entirely
            raise GitError("git executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git command timed out: {' '.join(args)}") from exc
        if check and proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): {err}",
                stderr=err,
            )
        return proc

    def out(self, args: list[str]) -> str:
        return _decode(self.run(args).stdout).strip()

    # -------------------------------------------------------------- queries
    def is_valid(self) -> bool:
        try:
            self.run(["rev-parse", "--git-dir"], check=True, timeout=30)
            return True
        except GitError:
            return False

    def is_bare(self) -> bool:
        if self._is_bare is None:
            try:
                self._is_bare = self.out(["rev-parse", "--is-bare-repository"]) == "true"
            except GitError:
                self._is_bare = False
        return self._is_bare

    def head_sha(self, ref: str = "HEAD") -> str | None:
        proc = self.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode().strip() or None

    def resolve_commit(self, ref: str) -> str | None:
        return self.head_sha(ref)

    def default_ref(self) -> str:
        proc = self.run(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        name = proc.stdout.decode("utf-8", "replace").strip()
        return name or "HEAD"

    def list_refs(self) -> list[dict]:
        """Branches and tags (annotated tags dereferenced to commits)."""
        proc = self.run(
            ["for-each-ref",
             "--format=%(refname:short)%00%(objecttype)%00%(objectname)",
             "refs/heads", "refs/tags"],
            check=False,
        )
        out: list[dict] = []
        for line in _decode(proc.stdout).splitlines():
            parts = line.split("\x00")
            if len(parts) != 3 or not parts[0]:
                continue
            out.append({"name": parts[0], "type": parts[1], "sha": parts[2]})
        return out

    def no_merge_commit_count(self, ref: str) -> int:
        proc = self.run(["rev-list", "--no-merges", "--count", ref], check=False)
        try:
            return int(proc.stdout.decode().strip())
        except ValueError:
            return 0

    def commit_dates_range(self, ref: str) -> tuple[int, int]:
        """(oldest, newest) committer timestamps, non-merge commits."""
        proc = self.run(
            ["log", ref, "--no-merges", "--format=%ct"],
            check=False,
        )
        vals = [int(x) for x in proc.stdout.decode().split() if x.isdigit()]
        if not vals:
            return (0, 0)
        return (min(vals), max(vals))

    def ls_tree_paths(self, ref: str) -> list[str]:
        """All blob paths in the tree at ref (used only for path listings)."""
        proc = self.run(["ls-tree", "-r", "-z", "--full-tree", "--name-only", ref], check=False)
        if proc.returncode != 0:
            return []
        return [_decode(p) for p in proc.stdout.split(b"\x00") if p]

    def raw_numstat_log(self, ref: str) -> bytes:
        """Full ``git log --numstat`` dump for non-merge commits reachable from ref.

        Format (verified against git 2.43):

        ``C\\x01<sha>\\x01<author name>\\x01<author email>\\x01<ct>\\x01<subject>\\n``
        followed by NUL separated entries:

        * ``<added>\\t<removed>\\t<path>\\0``                 (add / modify / delete)
        * ``<added>\\t<removed>\\t\\0<oldpath>\\0<newpath>\\0`` (rename, >= 50%)
        * ``-\\t-\\t<path>\\0``                              (binary: not measured)
        * ``C\\x01...<subject>\\0`` with no newline/entries    (empty commit)

        Rename detection is pinned at 50% and renames always carry the *content*
        delta only, so a pure rename produces 0/0 and does not disturb metrics.
        """
        args = [
            "log", ref, "--no-merges", "-z", "--numstat",
            "--find-renames=50%",
            "--pretty=format:C%x01%H%x01%an%x01%ae%x01%ct%x01%s",
        ]
        proc = self.run(args, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"failed to read history of {ref}: {err}", stderr=err)
        return proc.stdout

    # ------------------------------------------------------------- mailmap
    def has_mailmap(self, ref: str = "HEAD") -> bool:
        try:
            proc = self.run(["cat-file", "-e", f"{ref}:.mailmap"], check=False)
            if proc.returncode == 0:
                return True
        except GitError:
            pass
        if self.worktree is not None:
            mf = self.worktree / ".mailmap"
            if mf.is_file():
                return True
        return False

    def check_mailmap(self, identities: list[str]) -> dict[str, str]:
        """Map raw ``Name <email>`` identities through the repo's mailmap.

        Delegates to git itself (``git check-mailmap --stdin``) so every
        mailmap form/flavour git supports is handled identically.
        Returns a mapping only for identities whose canonical form differs.
        """
        if not identities:
            return {}
        payload = "\n".join(identities) + "\n"
        proc = self.run(
            ["check-mailmap", "--stdin"],
            input_bytes=payload.encode("utf-8", "surrogateescape"),
            check=False,
        )
        if proc.returncode != 0:
            return {}
        lines = _decode(proc.stdout).splitlines()
        if len(lines) != len(identities):
            return {}
        return {raw: mapped for raw, mapped in zip(identities, lines) if raw != mapped}


def identity_string(name: str, email: str) -> str:
    return f"{name} <{email}>"


def split_identity(ident: str) -> tuple[str, str]:
    """Split ``Name <email>``; tolerant of odd names."""
    lt = ident.rfind("<")
    if lt == -1:
        return ident.strip(), ""
    gt = ident.find(">", lt)
    name = ident[:lt].strip()
    email = ident[lt + 1:gt].strip() if gt != -1 else ident[lt + 1:].strip()
    return name, email
