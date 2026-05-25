"""Entry point: python -m kb [--port PORT] [--host HOST] [--reload]"""
from __future__ import annotations

import argparse

import uvicorn

from kb.config import get_settings


def main() -> None:
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Knowledge Base API server")
    parser.add_argument("--host", default=settings.server.host)
    parser.add_argument("--port", type=int, default=settings.server.port)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    uvicorn.run(
        "kb.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
