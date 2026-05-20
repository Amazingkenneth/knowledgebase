import pytest
from pydantic import ValidationError

from kb.models.document import AlarmDoc, SetupDoc


def _kw():
    return dict(
        project="FTU",
        equipment="Sphere",
        title="t",
        content="c",
        resolution="r",
    )


def test_error_codes_normalize_to_upper():
    doc = AlarmDoc(**_kw(), error_codes=["abc-1", "Z9"])
    assert doc.error_codes == ["ABC-1", "Z9"]


def test_error_codes_reject_bad_chars():
    with pytest.raises(ValidationError):
        AlarmDoc(**_kw(), error_codes=["bad code!"])


def test_error_codes_reject_dupes_after_normalize():
    with pytest.raises(ValidationError):
        AlarmDoc(**_kw(), error_codes=["abc", "ABC"])


def test_title_strip_and_required():
    with pytest.raises(ValidationError):
        AlarmDoc(**{**_kw(), "title": "   "})


def test_required_content_fields():
    with pytest.raises(ValidationError):
        AlarmDoc(**{**_kw(), "content": ""})
    with pytest.raises(ValidationError):
        AlarmDoc(**{**_kw(), "resolution": ""})


def test_setup_doc_sections_order():
    doc = SetupDoc(
        project="FTU",
        equipment="Sphere",
        title="setup x",
        procedure="P",
        prerequisites="pre",
        notes="n",
    )
    names = [n for n, _ in doc.content_sections()]
    assert names == ["prerequisites", "procedure", "notes"]
