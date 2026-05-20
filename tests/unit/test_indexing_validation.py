import pytest

from kb.models.document import AlarmDoc
from kb.models.taxonomy import KnowledgeType, Taxonomy
from kb.services.indexing import IndexingError, doc_id, validate_against_taxonomy


def _tax() -> Taxonomy:
    return Taxonomy(
        version="t",
        knowledge_types=[KnowledgeType.ALARM, KnowledgeType.SETUP],
        projects=["FTU", "MHK"],
        equipment=["Sphere", "LDI"],
    )


def _doc(**overrides) -> AlarmDoc:
    base = dict(
        project="FTU",
        equipment="Sphere",
        error_codes=["125002"],
        title="t",
        content="c",
        resolution="r",
    )
    base.update(overrides)
    return AlarmDoc(**base)


def test_validate_ok():
    validate_against_taxonomy(_doc(), _tax())


def test_validate_unknown_project():
    with pytest.raises(IndexingError, match="project"):
        validate_against_taxonomy(_doc(project="XYZ"), _tax())


def test_validate_unknown_equipment():
    with pytest.raises(IndexingError, match="equipment"):
        validate_against_taxonomy(_doc(equipment="Galaxy"), _tax())


def test_doc_id_is_stable():
    a = _doc(error_codes=["125002", "124000"])
    b = _doc(error_codes=["124000", "125002"])  # order-insensitive
    assert doc_id(a) == doc_id(b)
    assert doc_id(a).startswith("alarm:")


def test_doc_id_changes_with_content_identity():
    a = _doc()
    b = _doc(title="different")
    assert doc_id(a) != doc_id(b)
