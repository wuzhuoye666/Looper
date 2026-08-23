"""Development server entry point that honours Looper network settings."""

from __future__ import annotations

from pathlib import Path

import uvicorn

from looper_api.config import get_settings


def main() -> None:
    settings = get_settings()
    api_source_dir = Path(__file__).resolve().parent
    uvicorn.run(
        "looper_api.app:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        reload_dirs=[str(api_source_dir)],
    )


if __name__ == "__main__":
    main()
