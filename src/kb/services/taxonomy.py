from __future__ import annotations

from pathlib import Path
from threading import RLock

import yaml

from kb.models.taxonomy import Taxonomy


class TaxonomyStore:
    """Process-local taxonomy registry.

    Loaded from YAML at startup; reloadable via reload(). Thread-safe (admin
    endpoint may reload while a request reads).
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = RLock()
        self._taxonomy: Taxonomy = self._load()

    def _load(self) -> Taxonomy:
        if not self._path.exists():
            raise FileNotFoundError(f"taxonomy file not found: {self._path}")
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"{self._path}: top-level YAML must be a mapping")
        return Taxonomy(**raw)

    def reload(self) -> Taxonomy:
        with self._lock:
            self._taxonomy = self._load()
            return self._taxonomy

    @property
    def current(self) -> Taxonomy:
        with self._lock:
            return self._taxonomy
