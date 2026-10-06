"""Repository ingestion: deep clones and zip uploads.

Both entry points report progress through a callback and validate that the
result is a usable git repository before returning.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

from .config import MAX_EXTRACT_BYTES, MAX_ZIP_ENTRIES
from .gitrepo import GIT_BIN, GIT_ENV, GitRepo, _decode


class IngestError(RuntimeError):
    pass


ProgressFn = "callable | None"


def _report(progress, pct: float, message: str) -> None:
    if progress is not None:
        try:
            progress(max(0.0, min(100.0, pct)), message)
        except Exception:  # pragma: no cover - progress must never break ingestion
            pass


# --------------------------------------------------------------------------- #
# Clone
# --------------------------------------------------------------------------- #
_RECEIVING_RE = re.compile(rb"(Receiving objects|Resolving deltas|Compressing objects):\s+(\d+)%")
_COUNT_RE = re.compile(rb"(Enumerating objects|Counting objects):\s+(\d+)")


def clone_repo(url: str, dest: Path, progress=None) -> GitRepo:
    """Deep (full-history) mirror clone of ``url`` into ``dest`` (bare repo)."""
    url = (url or "").strip()
    if not url:
        raise IngestError("No repository URL provided.")
    if url.startswith("-"):
        raise IngestError("Invalid repository URL.")

    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    _report(progress, 0, f"Cloning {url} ...")
    cmd = [GIT_BIN, "-c", "safe.directory=*", "clone", "--mirror", "--progress", "--", url, str(dest)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=GIT_ENV,
    )
    tail: list[bytes] = []
    receiving_pct = 0.0
    resolving_pct = 0.0
    assert proc.stderr is not None
    for raw_line in proc.stderr:
        line = raw_line.strip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 60:
            tail.pop(0)
        m = _RECEIVING_RE.search(line)
        if m:
            stage, pct = m.group(1), float(m.group(2))
            if stage == b"Receiving objects":
                receiving_pct = pct
            else:
                resolving_pct = pct
            overall = receiving_pct * 0.8 + resolving_pct * 0.2
            _report(progress, overall, _decode(line))
            continue
        m = _COUNT_RE.search(line)
        if m:
            _report(progress, 1.0, _decode(line))
            continue
        text = _decode(line).removeprefix("remote: ").strip()
        if text:
            _report(progress, receiving_pct * 0.8, text)
    proc.wait()
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        err = _decode(b"\n".join(tail[-6:]))
        raise IngestError(f"Clone failed: {err or 'git exited with ' + str(proc.returncode)}")

    _report(progress, 100, "Clone complete, verifying repository ...")
    repo = GitRepo(gitdir=dest, worktree=None)
    if not repo.is_valid():
        shutil.rmtree(dest, ignore_errors=True)
        raise IngestError("The cloned directory is not a valid git repository.")
    return repo


# --------------------------------------------------------------------------- #
# Zip upload
# --------------------------------------------------------------------------- #
def _safe_zip_member(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    norm = name.replace("\\", "/")
    parts = norm.split("/")
    if any(p == ".." for p in parts):
        return False
    if re.match(r"^[A-Za-z]:", norm):  # windows drive letter
        return False
    return True


def extract_zip(zip_path: Path, dest: Path, progress=None) -> GitRepo:
    """Safely extract an uploaded zip and locate the git repository inside."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise IngestError("The uploaded file is not a valid zip archive.") from exc

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise IngestError("Archive contains too many entries.")
        total_size = sum(i.file_size for i in infos)
        if total_size > MAX_EXTRACT_BYTES:
            raise IngestError("Archive is too large to extract (over 8 GiB).")

        done = 0
        for info in infos:
            name = info.filename
            if info.is_dir() or not _safe_zip_member(name):
                if not info.is_dir() and not _safe_zip_member(name):
                    raise IngestError(f"Archive contains an unsafe path: {name!r}")
                continue
            done += info.file_size
            if progress is not None and len(infos) > 50:
                pct = 100.0 * done / max(total_size, 1)
                _report(progress, pct * 0.9, f"Extracting {name}")
            target = dest / name.replace("\\", "/")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1 << 20)

    _report(progress, 92, "Locating git repository ...")
    repo = locate_git_repo(dest)
    if repo is None:
        raise IngestError(
            "No git repository found in the archive. The zip must contain the "
            "repository's .git directory (or be a bare repository)."
        )
    return repo


def locate_git_repo(root: Path, max_depth: int = 3) -> GitRepo | None:
    """Find the git repository inside an extracted directory tree."""
    # The extracted directory itself may be a bare repository.
    if (root / "HEAD").is_file() and (root / "objects").is_dir():
        repo = GitRepo(gitdir=root, worktree=None)
        if repo.is_valid():
            return repo

    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        git_entry = current / ".git"
        if git_entry.is_dir():
            repo = GitRepo(gitdir=git_entry, worktree=current)
            if repo.is_valid():
                return repo
        elif git_entry.is_file():
            # worktree-style gitfile: "gitdir: <path>"
            try:
                content = git_entry.read_text("utf-8", "replace").strip()
                if content.lower().startswith("gitdir:"):
                    target = Path(content.split(":", 1)[1].strip())
                    if not target.is_absolute():
                        target = (current / target).resolve()
                    if target.exists():
                        repo = GitRepo(gitdir=target, worktree=current)
                        if repo.is_valid():
                            return repo
            except OSError:
                pass

        if depth < max_depth:
            try:
                for child in sorted(current.iterdir()):
                    if child.is_dir() and child.name not in {".git"}:
                        queue.append((child, depth + 1))
            except OSError:
                continue

    # Fallback: a directory that contains bare-git layout a level down.
    for candidate in root.rglob("HEAD"):
        parent = candidate.parent
        if (parent / "objects").is_dir() and (parent / "refs").is_dir():
            repo = GitRepo(gitdir=parent, worktree=None)
            if repo.is_valid():
                return repo
    return None


def find_uncommitted_warning(repo: GitRepo) -> str | None:
    """Human friendly note for archives whose worktree differs from history."""
    if repo.worktree is None:
        return None
    try:
        proc = repo.run(["status", "--porcelain", "--untracked-files=no"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return "Working tree has uncommitted changes; metrics reflect the commit history only."
    except Exception:
        return None
    return None


def cleanup_path(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def disk_usage_mb(path: Path) -> float:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return round(total / (1024 * 1024), 2)
