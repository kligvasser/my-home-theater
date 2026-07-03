"""Entry point: run the dashboard/API server."""

from __future__ import annotations

import os

import uvicorn

from .logging_setup import configure_logging


def main() -> None:
    configure_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_logs=os.environ.get("LOG_JSON", "").lower() in {"1", "true", "yes"},
    )
    uvicorn.run(
        "homeTheater.api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - bind LAN by default
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "").lower() in {"1", "true", "yes"},
    )


if __name__ == "__main__":
    main()
