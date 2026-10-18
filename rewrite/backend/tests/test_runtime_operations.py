import importlib
from pathlib import Path

import psycopg

from helpdesk.settings import Settings


def test_empty_template_values_keep_optional_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("HD_MODEL_CA_FILE", "")
    monkeypatch.setenv("HD_CHAT_REQUEST_MODEL", "")
    settings = Settings(database_url="postgresql://unused", upload_dir=tmp_path)
    assert settings.model_ca_file is None
    assert settings.chat_request_model == ""


def runtime_check(monkeypatch, dsn, tmp_path):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / "tools"))
    monkeypatch.setenv("HD_DATABASE_URL", dsn)
    monkeypatch.setenv("HD_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("HD_MODEL_INFERENCE_ENABLED", "false")
    monkeypatch.delenv("HD_MODEL_CA_FILE", raising=False)
    return importlib.import_module("check_runtime")


def test_runtime_check_uses_migrated_database_without_disclosing_dsn(
    database, monkeypatch, tmp_path, capsys
):
    check = runtime_check(monkeypatch, database, tmp_path)
    assert check.main() == 0
    output = capsys.readouterr().out
    assert "schema 就绪" in output
    assert "未启用" in output
    assert database not in output


def test_runtime_check_never_initializes_an_empty_database(
    empty_database, monkeypatch, tmp_path, capsys
):
    check = runtime_check(monkeypatch, empty_database, tmp_path)
    assert check.main() == 1
    assert "Migrate" in capsys.readouterr().out
    with psycopg.connect(empty_database) as conn:
        assert conn.execute("SELECT to_regclass('app_migrations')").fetchone()[0] is None


def test_runtime_check_rejects_missing_ca_before_connecting(
    monkeypatch, tmp_path, capsys
):
    check = runtime_check(monkeypatch, "postgresql://invalid/private-secret", tmp_path)
    monkeypatch.setenv("HD_MODEL_CA_FILE", str(tmp_path / "absent.pem"))
    assert check.main() == 1
    output = capsys.readouterr().out
    assert "CA 文件不存在" in output
    assert "private-secret" not in output
