"""Tracks imported files by content hash for duplicate detection and auto-restore.

Each file is identified by the SHA-256 of its content. The full ES source
payloads of committed documents are stored alongside, enabling restore_imports()
to re-index them after a CSV seed clears the main indices.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from elasticsearch import AsyncElasticsearch, NotFoundError
from kb.es.import_mappings import IMPORT_INDEX_NAME

log = logging.getLogger("kb.file_tracker")


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FileTracker:
    def __init__(self, es: AsyncElasticsearch):
        self._es = es

    async def exists(self, file_hash: str) -> dict[str, Any] | None:
        """Return the import record if this file hash was previously imported, else None."""
        try:
            resp = await self._es.get(index=IMPORT_INDEX_NAME, id=file_hash)
            return resp["_source"]
        except NotFoundError:
            return None

    async def check_duplicates(self, file_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Check multiple hashes at once. Returns {hash: {exists, last_imported}} for found ones."""
        if not file_hashes:
            return {}
        result: dict[str, dict[str, Any]] = {}
        resp = await self._es.mget(
            index=IMPORT_INDEX_NAME,
            body={"ids": file_hashes},
        )
        for doc in resp.get("docs", []):
            fh = doc["_id"]
            if doc.get("found"):
                src = doc["_source"]
                result[fh] = {
                    "exists": True,
                    "last_imported": src.get("updated_at") or src.get("created_at"),
                    "import_status": src.get("import_status"),
                }
            else:
                result[fh] = {"exists": False}
        return result

    async def record_pending(
        self,
        file_hash: str,
        file_name: str,
        file_path: str,
        file_size: int,
        file_type: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self._es.index(
            index=IMPORT_INDEX_NAME,
            id=file_hash,
            document={
                "file_hash": file_hash,
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "file_type": file_type,
                "import_status": "pending",
                "committed_docs": [],
                "error_message": "",
                "created_at": now,
                "updated_at": now,
            },
            refresh="wait_for",
        )

    async def record_committed(
        self,
        file_hash: str,
        committed_docs: list[dict[str, Any]],
    ) -> None:
        """Update the import record with committed document payloads."""
        now = datetime.now(UTC).isoformat()
        await self._es.update(
            index=IMPORT_INDEX_NAME,
            id=file_hash,
            body={
                "doc": {
                    "import_status": "committed",
                    "committed_docs": committed_docs,
                    "updated_at": now,
                }
            },
            refresh="wait_for",
        )

    async def record_failed(self, file_hash: str, error_message: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            await self._es.update(
                index=IMPORT_INDEX_NAME,
                id=file_hash,
                body={
                    "doc": {
                        "import_status": "failed",
                        "error_message": error_message,
                        "updated_at": now,
                    }
                },
                refresh="wait_for",
            )
        except NotFoundError:
            log.warning("file_tracker: cannot mark %s as failed — record not found", file_hash[:12])

    async def get_all_committed(self) -> list[dict[str, Any]]:
        """Return all committed document payloads for auto-restore."""
        all_docs: list[dict[str, Any]] = []
        try:
            resp = await self._es.search(
                index=IMPORT_INDEX_NAME,
                body={
                    "query": {"term": {"import_status": "committed"}},
                    "size": 10000,
                    "_source": ["committed_docs"],
                },
            )
            for hit in resp["hits"]["hits"]:
                docs = hit["_source"].get("committed_docs", [])
                all_docs.extend(docs)
        except Exception as exc:
            log.warning("file_tracker: could not load committed docs — %s", exc)
        return all_docs
