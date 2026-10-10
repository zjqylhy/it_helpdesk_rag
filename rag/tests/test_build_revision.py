import subprocess

from helpdesk.evaluation.runner import git_commit


def test_container_revision_without_git(monkeypatch):
    monkeypatch.setenv('APP_REVISION', 'abc123-dirty')
    def missing_git(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(subprocess, 'check_output', missing_git)
    assert git_commit() == 'abc123-dirty'


def test_native_revision_fallback(monkeypatch):
    monkeypatch.delenv('APP_REVISION', raising=False)
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: 'native-commit\n')
    assert git_commit() == 'native-commit'


def test_missing_revision_is_explicit(monkeypatch):
    monkeypatch.delenv('APP_REVISION', raising=False)
    def missing_git(*args, **kwargs):
        raise FileNotFoundError('git')
    monkeypatch.setattr(subprocess, 'check_output', missing_git)
    assert git_commit() == 'unknown'
