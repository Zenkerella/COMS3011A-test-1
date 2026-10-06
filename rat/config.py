"""Global configuration for the RAT server."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Directory holding all runtime state (cloned/extracted repositories, caches,
# the registry file).  Overridable so the tool can run in an ephemeral env.
DATA_DIR = Path(os.environ.get("RAT_DATA_DIR", str(BASE_DIR / "data"))).resolve()
REGISTRY_FILE = DATA_DIR / "registry.json"
WEB_DIR = BASE_DIR / "web"

# Bump whenever the on-disk parse cache layout changes.
# v5: empty-diff commits (bare header tokens) are no longer dropped.
CACHE_VERSION = 5

# Identity / rename handling used when reading history.
RENAME_THRESHOLD = "50%"

# Guards for uploaded archives.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024      # 2 GiB
MAX_EXTRACT_BYTES = 8 * 1024 * 1024 * 1024      # 8 GiB
MAX_ZIP_ENTRIES = 400_000

# How many parsed repository histories to keep hot in memory.
MAX_HOT_REPOS = 4
# How many metric query results to memoise per repository.
QUERY_CACHE_SIZE = 64
