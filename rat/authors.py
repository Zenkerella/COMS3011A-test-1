"""Author identity handling: mailmap merging and manual merges.

``git check-mailmap`` performs the .mailmap translation so every mailmap form
git supports works exactly as git intends.  Manual merges (for repositories
without a mailmap) are applied on top of the mailmapped identities via
union-find, so both mechanisms compose cleanly.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from .gitrepo import GitRepo, split_identity


@dataclass
class AuthorState:
    """Cluster assignment of commits to displayable authors."""

    author_ids: np.ndarray                 # int32 per commit (chronological)
    authors: list[dict]                    # index == author id
    version: int = 0

    def by_id(self, aid: int) -> dict | None:
        if 0 <= aid < len(self.authors):
            return self.authors[aid]
        return None


def load_mailmap_map(gitrepo: GitRepo, ref: str, raw_ids: list[str]) -> dict[str, str]:
    """Mapping raw identity -> canonical identity for identities the mailmap rewrites."""
    unique = sorted(set(raw_ids))
    if not unique:
        return {}
    try:
        return gitrepo.check_mailmap(unique)
    except Exception:
        return {}


def _find(parent: dict[str, str], x: str) -> str:
    root = x
    while parent[root] != root:
        root = parent[root]
    while parent[x] != root:  # path compression
        parent[x], x = root, parent[x]
    return root


def build_author_state(raw_ids: list[str], mailmap_map: dict[str, str],
                       merge_groups: list[list[str]], version: int = 0) -> AuthorState:
    """Cluster commits into authors.

    ``merge_groups`` contains lists of canonical (post-mailmap) identity keys
    that the user explicitly merged together.
    """
    n = len(raw_ids)
    canonical = [mailmap_map.get(r, r) for r in raw_ids]
    uniq_canonical = sorted(set(canonical))

    parent: dict[str, str] = {c: c for c in uniq_canonical}
    for group in merge_groups or []:
        members = [g for g in group if g in parent]
        for other in members[1:]:
            ra, rb = _find(parent, members[0]), _find(parent, other)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)  # deterministic root

    # group canonical keys per cluster
    clusters: dict[str, list[str]] = {}
    for c in uniq_canonical:
        clusters.setdefault(_find(parent, c), []).append(c)

    counts = Counter(canonical)

    # aliases: raw identity -> commits (for the merge UI)
    raw_counts = Counter(raw_ids)
    raw_to_root = {raw: _find(parent, mailmap_map.get(raw, raw)) for raw in raw_counts}
    root_aliases: dict[str, list[dict]] = {root: [] for root in clusters}
    for raw, cnt in raw_counts.items():
        nm, em = split_identity(raw)
        root_aliases[raw_to_root[raw]].append({
            "name": nm, "email": em, "commits": cnt,
            "canonical": mailmap_map.get(raw, raw),
        })

    root_order = sorted(clusters.keys())
    root_to_aid = {root: i for i, root in enumerate(root_order)}
    authors: list[dict] = []
    for root in root_order:
        members = clusters[root]
        members.sort(key=lambda c: (-counts[c], c))
        rep = members[0]
        alias_rows = root_aliases[root]
        alias_rows.sort(key=lambda a: (-a["commits"], a["name"]))
        name, email = split_identity(rep)
        commits = sum(counts[m] for m in members)
        mailmapped = any(a["canonical"] != f'{a["name"]} <{a["email"]}>' for a in alias_rows)
        merged = len(members) > 1 or len(alias_rows) > 1
        authors.append({
            "id": root_to_aid[root],
            "name": name,
            "email": email,
            "commits": commits,
            "aliases": alias_rows,
            "keys": sorted(members),
            "mailmapped": mailmapped,
            "merged": merged,
        })

    if n:
        author_ids = np.fromiter(
            (root_to_aid[_find(parent, c)] for c in canonical),
            dtype=np.int32, count=n,
        )
    else:
        author_ids = np.zeros(0, dtype=np.int32)

    return AuthorState(author_ids=author_ids, authors=authors, version=version)


def sanitize_merge_groups(groups, known_keys: set[str]) -> list[list[str]]:
    """Keep only merge groups with >=2 distinct known canonical keys."""
    out: list[list[str]] = []
    for group in groups or []:
        if not isinstance(group, list):
            continue
        seen: list[str] = []
        for key in group:
            if isinstance(key, str) and key in known_keys and key not in seen:
                seen.append(key)
        if len(seen) >= 2:
            out.append(seen)
    return out
