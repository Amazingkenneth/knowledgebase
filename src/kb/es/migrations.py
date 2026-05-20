"""Index lifecycle management.

CLI:
    python -m kb.es.migrations create-all          # create all knowledge_type indices at v1
    python -m kb.es.migrations create alarm        # create just alarm at next version
    python -m kb.es.migrations status              # show alias → index mapping
    python -m kb.es.migrations reindex alarm 1 2   # reindex v1 -> v2 and swap alias

Versioned indices (kb_alarm_v1) behind a stable alias (kb_alarm) — atomic swap
on reindex avoids downtime when the mapping or embedding model changes.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from elasticsearch import AsyncElasticsearch, NotFoundError

from kb.config import Settings, get_settings
from kb.es.client import close_es, get_es
from kb.es.mappings import alias_name, all_alias_pattern, index_body, index_name
from kb.models.taxonomy import KnowledgeType

_VERSION_SUFFIX_RE = re.compile(r"_v(\d+)$")


async def _next_version(es: AsyncElasticsearch, prefix: str, kt: KnowledgeType) -> int:
    pattern = f"{prefix}_{kt.value}_v*"
    resp = await es.indices.get(index=pattern, ignore_unavailable=True)
    if not resp:
        return 1
    versions = []
    for name in resp:
        m = _VERSION_SUFFIX_RE.search(name)
        if m:
            versions.append(int(m.group(1)))
    return (max(versions) if versions else 0) + 1


async def create_one(es: AsyncElasticsearch, settings: Settings, kt: KnowledgeType) -> str:
    version = await _next_version(es, settings.es.index_prefix, kt)
    name = index_name(settings.es.index_prefix, kt, version)
    alias = alias_name(settings.es.index_prefix, kt)
    body = index_body(settings.embedding.dims, settings.es.analyzer_index, settings.es.analyzer_query)
    await es.indices.create(index=name, **body)  # type: ignore[arg-type]
    # Point the alias at the new index.
    # If a previous version of this alias exists on another index, swap it atomically.
    actions = [{"add": {"index": name, "alias": alias}}]
    try:
        existing = await es.indices.get_alias(name=alias)
        for existing_index in existing:
            if existing_index != name:
                actions.append({"remove": {"index": existing_index, "alias": alias}})
    except NotFoundError:
        pass
    await es.indices.update_aliases(actions=actions)
    return name


async def create_all(es: AsyncElasticsearch, settings: Settings) -> dict[str, str]:
    created: dict[str, str] = {}
    for kt in KnowledgeType:
        created[kt.value] = await create_one(es, settings, kt)
    return created


async def status(es: AsyncElasticsearch, settings: Settings) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        aliases = await es.indices.get_alias(index=all_alias_pattern(settings.es.index_prefix))
    except NotFoundError:
        return out
    for index, body in aliases.items():
        for alias in body.get("aliases", {}):
            out.setdefault(alias, []).append(index)
    return out


async def reindex(
    es: AsyncElasticsearch,
    settings: Settings,
    kt: KnowledgeType,
    src_version: int,
    dst_version: int,
) -> None:
    src = index_name(settings.es.index_prefix, kt, src_version)
    dst = index_name(settings.es.index_prefix, kt, dst_version)
    alias = alias_name(settings.es.index_prefix, kt)

    if not await es.indices.exists(index=dst):
        await es.indices.create(index=dst, **index_body(settings.embedding.dims, settings.es.analyzer_index, settings.es.analyzer_query))  # type: ignore[arg-type]

    await es.reindex(source={"index": src}, dest={"index": dst}, refresh=True)
    await es.indices.update_aliases(
        actions=[
            {"remove": {"index": src, "alias": alias}},
            {"add": {"index": dst, "alias": alias}},
        ]
    )


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="kb.es.migrations")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-all")
    create = sub.add_parser("create")
    create.add_argument("knowledge_type", choices=[kt.value for kt in KnowledgeType])
    sub.add_parser("status")
    re_p = sub.add_parser("reindex")
    re_p.add_argument("knowledge_type", choices=[kt.value for kt in KnowledgeType])
    re_p.add_argument("src", type=int)
    re_p.add_argument("dst", type=int)

    args = parser.parse_args(argv)
    settings = get_settings()
    es = get_es(settings)
    try:
        if args.cmd == "create-all":
            res = await create_all(es, settings)
            for kt, name in res.items():
                print(f"created {name} (alias {settings.es.index_prefix}_{kt})")
        elif args.cmd == "create":
            kt = KnowledgeType(args.knowledge_type)
            name = await create_one(es, settings, kt)
            print(f"created {name}")
        elif args.cmd == "status":
            mapping = await status(es, settings)
            if not mapping:
                print("(no aliases found)")
            for alias, indices in mapping.items():
                print(f"{alias} -> {', '.join(indices)}")
        elif args.cmd == "reindex":
            kt = KnowledgeType(args.knowledge_type)
            await reindex(es, settings, kt, args.src, args.dst)
            print(f"reindexed {kt.value} v{args.src} -> v{args.dst}, alias swapped")
    finally:
        await close_es()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
