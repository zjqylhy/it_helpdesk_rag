import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from helpdesk.adapters.http.app import create_app
from helpdesk.domain.contracts import AnswerCheck, ChatRequest, CoverageReport, PublicEvent
from helpdesk.settings import Settings


def export(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    models: dict[str, type[BaseModel]] = {
        "coverage-v1": CoverageReport,
        "answer-check-v1": AnswerCheck,
        "chat-request-v1": ChatRequest,
        "public-event-envelope-v1": PublicEvent,
    }
    for name, model in models.items():
        schema = model.model_json_schema(by_alias=True)
        (directory / (name + ".schema.json")).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # 仅装配路由，不执行 lifespan、不建立数据库连接。
    app = create_app(Settings(database_url="postgresql://unused/unused", upload_dir=Path("unused")))  # type: ignore[arg-type]
    (directory / "openapi.json").write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    export(parser.parse_args().directory)


if __name__ == "__main__":
    main()
