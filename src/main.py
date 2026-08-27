"""VectorShelf process entry point; all product configuration comes from settings."""

from __future__ import annotations

from src.infra.runtime import create_asgi_application
from src.infra.settings import load_settings


def main() -> None:
    settings = load_settings()
    app = create_asgi_application(settings)
    import uvicorn

    uvicorn.run(app, port=int(settings["server.port"]))


if __name__ == "__main__":
    main()
