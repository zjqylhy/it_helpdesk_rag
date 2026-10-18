import uvicorn

from helpdesk.adapters.http.app import create_app
from helpdesk.entrypoints.runtime import run
from helpdesk.settings import Settings


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    config = uvicorn.Config(
        create_app(settings), host=settings.host, port=settings.port, workers=1, access_log=False
    )
    run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
