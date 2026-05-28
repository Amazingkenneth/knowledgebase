"""Tests for the knowledge-type spec system.

Guarantees:
  - Every KnowledgeType has a spec YAML.
  - Spec field lists declare every required pydantic field on the matching
    document model — drift between the LLM contract and storage schema is
    caught at test time, not commit time.
  - The rendered prompts include the field names + example output.
  - Router prompt enumerates every type plus "skip".
"""
from __future__ import annotations

import pytest

from kb.models.document import doc_class_for
from kb.models.taxonomy import KnowledgeType
from kb.services.spec import (
    load_specs,
    render_router_prompt,
    render_segmentation_prompt,
)


@pytest.fixture(scope="module")
def specs():
    load_specs.cache_clear()
    return load_specs()


def test_every_type_has_a_spec(specs):
    assert set(specs) == set(KnowledgeType)


@pytest.mark.parametrize("kt", list(KnowledgeType))
def test_spec_fields_cover_required_model_fields(specs, kt):
    """Every required pydantic field (other than knowledge_type / audit /
    boilerplate) must be representable from the spec — either directly named
    in spec.fields or via a derived mapping in segmentation._parsed_to_staged.

    We assert the spec lists every field used to populate required model
    fields. The mapping is:
      alarm:      error_code -> error_codes, title, content, resolution
      setup:      station    -> title,       procedure
      experience: problem    -> title,       failure_desc -> body_text
    """
    spec = specs[kt]
    spec_names = {f.name for f in spec.fields}
    # AlarmDoc.title is derived from title_zh / title_en in _parsed_to_staged,
    # so the spec only needs to expose the source fields, not the derived one.
    expected_by_type = {
        KnowledgeType.ALARM: {"error_code", "title_zh", "title_en", "content", "resolution"},
        KnowledgeType.SETUP: {"station", "procedure"},
        KnowledgeType.EXPERIENCE: {"problem", "failure_desc"},
    }
    missing = expected_by_type[kt] - spec_names
    assert not missing, f"spec {kt.value}.yaml missing required fields: {missing}"


@pytest.mark.parametrize("kt", list(KnowledgeType))
def test_required_spec_fields_are_marked(specs, kt):
    spec = specs[kt]
    required = set(spec.required_fields)
    expected = {
        KnowledgeType.ALARM: {"error_code", "content", "resolution"},
        KnowledgeType.SETUP: {"station", "procedure"},
        KnowledgeType.EXPERIENCE: {"problem", "failure_desc"},
    }[kt]
    assert expected <= required


@pytest.mark.parametrize("kt", list(KnowledgeType))
def test_rendered_prompt_contains_fields_and_example(specs, kt):
    spec = specs[kt]
    prompt = render_segmentation_prompt(spec)
    for f in spec.fields:
        assert f.name in prompt, f"prompt missing field {f.name}"
    assert "Example input" in prompt
    assert "Example output" in prompt
    assert "JSON array" in prompt
    # Skip rules must be visible to the LLM so non-content pages can be filtered.
    for rule in spec.skip_if:
        assert rule.split(",")[0] in prompt or rule in prompt


def test_router_prompt_lists_all_types_plus_skip(specs):
    prompt = render_router_prompt(specs)
    for kt in KnowledgeType:
        assert f'"{kt.value}"' in prompt
    assert '"skip"' in prompt


def test_example_output_round_trips_to_doc(specs):
    """The example_output in each spec should be a plausible LLM emission —
    i.e. converting it via _parsed_to_staged should not blow up. This is the
    closest thing to a contract test between the prompt and the storage layer."""
    from kb.services.segmentation import _parsed_to_staged

    for kt, spec in specs.items():
        assert spec.example_output, f"{kt.value}: example_output is empty"
        for i, entry in enumerate(spec.example_output):
            staged = _parsed_to_staged(
                i, entry, kt, "example.pdf", None, None,
                normalized_chunk_text="", normalized_full_raw="",
            )
            # Required fields should be populated from the example.
            assert staged.title, f"{kt.value} example {i}: empty title"
            # Knowledge-type-specific required field must be non-empty.
            cls = doc_class_for(kt)
            required = {
                name for name, info in cls.model_fields.items()
                if info.is_required() and name not in {"knowledge_type", "title"}
            }
            for field in required:
                val = getattr(staged, field, None)
                # error_codes is a list; the alarm example must yield at least one code.
                if field == "error_codes":
                    assert staged.error_codes, f"{kt.value}: example produced no error_codes"
                elif isinstance(val, str):
                    assert val and val != "—", f"{kt.value}: example produced empty {field}"
