param([switch]$Browser)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $taskRoot
try {
    $taskPython = Join-Path $taskRoot 'rewrite/backend/.venv/Scripts/python.exe'
    $taskInterpreter = if (Test-Path -LiteralPath $taskPython) { $taskPython } else { '3.12' }
    uv sync --locked --project rewrite/backend --python $taskInterpreter --cache-dir data/rewrite-uv-cache
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖校验失败' }
    & $taskPython -m ruff check rewrite/backend/src rewrite/backend/tests --config rewrite/backend/pyproject.toml
    if ($LASTEXITCODE -ne 0) { throw 'Ruff 检查失败' }
    & $taskPython -m mypy --config-file rewrite/backend/pyproject.toml rewrite/backend/src
    if ($LASTEXITCODE -ne 0) { throw '类型检查失败' }
    & $taskPython -m pytest rewrite/backend/tests -q --tb=short
    if ($LASTEXITCODE -ne 0) { throw '测试失败' }
    npm --prefix rewrite/frontend ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw '前端依赖校验失败' }
    npm --prefix rewrite/frontend run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
    npm --prefix rewrite/frontend test
    if ($LASTEXITCODE -ne 0) { throw '前端协议测试失败' }
    if ($Browser) {
        & $taskPython rewrite/scripts/verify_browser.py
        if ($LASTEXITCODE -ne 0) { throw '浏览器验收失败' }
    }
} finally { Pop-Location }
