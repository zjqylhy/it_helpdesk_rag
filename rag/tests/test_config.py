from helpdesk.config import Settings


def test_qwen_environment_and_canonical_priority(monkeypatch, tmp_path):
    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    monkeypatch.setenv('QWEN_API_KEY', 'test-alias-key')
    config = tmp_path / '.env'
    config.write_text('DASHSCOPE_API_KEY=\n', encoding='utf-8')
    # 空模板值不能盖过用户注入的别名；标准名称出现时仍优先。
    assert Settings(_env_file=config).dashscope_api_key == 'test-alias-key'
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'test-canonical-key')
    assert Settings(_env_file=config).dashscope_api_key == 'test-canonical-key'


def test_programmatic_credentials_override_environment(monkeypatch):
    monkeypatch.setenv('QWEN_API_KEY', 'test-alias-key')
    assert Settings(_env_file=None, dashscope_api_key='test-explicit-key').dashscope_api_key == 'test-explicit-key'
