"""Development server entry point that honours Looper network settings."""

from __future__ import annotations

import uvicorn

from looper_api.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "looper_api.app:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        reload_excludes=[".uv-cache/**", ".venv/**", "node_modules/**", "dist/**"],
    )


if __name__ == "__main__":
    main()
