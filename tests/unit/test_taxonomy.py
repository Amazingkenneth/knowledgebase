from pathlib import Path

import pytest
from pydantic import ValidationError

from kb.models.taxonomy import KnowledgeType, Taxonomy
from kb.services.taxonomy import TaxonomyStore


def test_taxonomy_rejects_blank():
    with pytest.raises(ValidationError):
        Taxonomy(
            version="v",
            knowledge_types=[KnowledgeType.ALARM],
            projects=[" "],
            equipment=["X"],
        )


def test_taxonomy_rejects_dupes():
    with pytest.raises(ValidationError):
        Taxonomy(
            version="v",
            knowledge_types=[KnowledgeType.ALARM],
            projects=["A", "A"],
            equipment=["X"],
        )


def test_taxonomy_membership_helpers():
    t = Taxonomy(
        version="v",
        knowledge_types=[KnowledgeType.ALARM],
        projects=["MHK", "MEM"],
        equipment=["FTU"],
    )
    assert t.has_project("MHK")
    assert not t.has_project("XYZ")
    assert t.has_equipment("FTU")
    assert not t.has_equipment("Sphere")


def test_taxonomy_store_loads_from_repo_yaml():
    path = Path(__file__).resolve().parents[2] / "config" / "taxonomy.yaml"
    assert path.exists(), f"missing fixture: {path}"
    store = TaxonomyStore(path)
    tax = store.current
    assert "MHK" in tax.projects
    assert "FTU" in tax.equipment
    assert KnowledgeType.ALARM in tax.knowledge_types
