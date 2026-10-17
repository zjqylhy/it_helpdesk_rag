"""只打包新实现与规格，不把本机配置、凭据、私有评测集或旧实现带到构建上下文。"""

import argparse
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXCLUDED = {
    ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "dist", "test-results", "playwright-report",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    files = sorted(
        p for folder in (ROOT / "rewrite", ROOT / "docs/redesign") for p in folder.rglob("*")
        if p.is_file() and not EXCLUDED.intersection(p.relative_to(ROOT).parts)
        and not p.name.startswith(".env") and p.suffix not in {".pyc", ".log"}
    )
    manifest = {
        "base_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_clean": not bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
        "files": {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, "x:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode()
        info = tarfile.TarInfo("deployment-source.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    print(json.dumps({"archive": str(args.output.resolve()), "files": len(files),
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
