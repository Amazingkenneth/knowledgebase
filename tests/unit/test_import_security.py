"""Security regression tests for the import pipeline.

These don't touch ES or any optional extraction libs — they exercise the pure
filename-sanitization helper that guards file writes against path traversal.
"""

from pathlib import Path

import pytest

from kb.services.import_pipeline import _safe_upload_path


def test_safe_upload_path_keeps_plain_name_inside_dir(tmp_path: Path):
    dest = _safe_upload_path(tmp_path, "abc123", "alarms.pdf")
    assert dest.parent == tmp_path.resolve()
    assert dest.name == "abc123_alarms.pdf"


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/cron.d/evil",
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\evil.bat",
        "sub/dir/escape.pdf",
    ],
)
def test_safe_upload_path_strips_traversal(tmp_path: Path, hostile: str):
    dest = _safe_upload_path(tmp_path, "h", hostile)
    # Whatever the input, the write target stays directly inside upload_dir.
    assert dest.parent == tmp_path.resolve()
    assert dest.resolve().is_relative_to(tmp_path.resolve())
    assert "/" not in dest.name.removeprefix("h_")
    assert ".." not in dest.name


def test_safe_upload_path_falls_back_for_empty_name(tmp_path: Path):
    dest = _safe_upload_path(tmp_path, "h", "...")
    assert dest.parent == tmp_path.resolve()
    assert dest.name == "h_upload"
