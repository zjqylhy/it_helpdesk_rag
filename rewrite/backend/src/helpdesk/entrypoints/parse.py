import base64
import json
import sys
from dataclasses import asdict

from helpdesk.adapters.parsing.paragraphs import parse
from helpdesk.domain.models import Row


def main() -> None:
    value: Row
    try:
        payload = json.loads(sys.stdin.buffer.read(29 * 1024 * 1024))
        chunks = parse(base64.b64decode(payload["data"], validate=True), payload["filename"])
        value = {"chunks": [asdict(c) for c in chunks]}
    except Exception:
        value = {"error": "parse_failed"}
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
