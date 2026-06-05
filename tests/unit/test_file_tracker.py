"""Unit tests for FileTracker's durability invariant.

The key invariant: the `committed` payload (`committed_docs`) is owned solely by
`record_committed`. `record_pending` and `record_failed` must never erase it —
otherwise a force re-import that the user abandons (or that fails) would drop a
previously-good import out of the tracker and lose it on the next reseed.

These tests assert the *mechanism* (atomic scripted updates guarding committed
rows) with a mocked ES client; the live ES round-trip is covered separately.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kb.services.file_tracker import FileTracker


@pytest.mark.asyncio
async def test_record_pending_upserts_and_guards_committed() -> None:
    es = AsyncMock()
    tracker = FileTracker(es)
    await tracker.record_pending(
        file_hash="h", file_name="f.docx", file_path="/tmp/f.docx",
        file_size=10, file_type="docx",
    )
    # Must be an atomic scripted upsert, not a full-document index() replace.
    es.index.assert_not_called()
    es.update.assert_awaited_once()
    kwargs = es.update.await_args.kwargs
    assert "script" in kwargs and "upsert" in kwargs
    # The script leaves an already-committed row untouched.
    assert "import_status == 'committed'" in kwargs["script"]["source"]
    assert "ctx.op = 'noop'" in kwargs["script"]["source"]
    # A brand-new row is inserted as pending via the upsert body.
    assert kwargs["upsert"]["import_status"] == "pending"
    assert kwargs["upsert"]["committed_docs"] == []


@pytest.mark.asyncio
async def test_record_failed_does_not_downgrade_committed() -> None:
    es = AsyncMock()
    tracker = FileTracker(es)
    await tracker.record_failed("h", "boom")
    es.update.assert_awaited_once()
    src = es.update.await_args.kwargs["script"]["source"]
    assert "import_status == 'committed'" in src
    assert "ctx.op = 'noop'" in src
    assert "ctx._source.import_status = 'failed'" in src
