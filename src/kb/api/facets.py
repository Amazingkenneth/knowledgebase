from __future__ import annotations

from fastapi import APIRouter

from kb.api.deps import TaxonomyDep
from kb.models.taxonomy import Taxonomy

router = APIRouter(prefix="/api/v1", tags=["facets"])


@router.get("/facets", response_model=Taxonomy)
async def facets(store: TaxonomyDep) -> Taxonomy:
    """Return the live taxonomy.

    Two consumers (see plan):
      1. Upstream LLM prompt priming — so it can disambiguate which token is
         a project vs equipment.
      2. Indexing-time validation — same registry, single source of truth.
    """
    return store.current


@router.post("/admin/reload-taxonomy", response_model=Taxonomy)
async def reload_taxonomy(store: TaxonomyDep) -> Taxonomy:
    return store.reload()
