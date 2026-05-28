"""Elasticsearch index mapping for the file import tracker (kb_import_files).

Stores file hashes, import status, and full committed document payloads
so that imported documents can be auto-restored after CSV seed clears indices.
"""

from __future__ import annotations

from typing import Any

IMPORT_INDEX_NAME = "kb_import_files"

IMPORT_INDEX_BODY: dict[str, Any] = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "dynamic": "strict",
        "properties": {
            "file_hash": {"type": "keyword"},
            "file_name": {"type": "keyword"},
            "file_path": {"type": "keyword"},
            "file_size": {"type": "long"},
            "file_type": {"type": "keyword"},
            "import_status": {"type": "keyword"},
            "committed_docs": {
                "type": "nested",
                "properties": {
                    "_index": {"type": "keyword"},
                    "_id": {"type": "keyword"},
                    "_source": {"type": "object", "enabled": False},
                },
            },
            "error_message": {"type": "text", "index": False},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
        },
    },
}
