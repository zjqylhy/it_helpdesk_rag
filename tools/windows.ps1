param(
    [ValidateSet('Init', 'Setup', 'Check', 'Run', 'Migrate', 'Bootstrap', 'Verify')]
    [string]$Action = 'Check',
    [ValidateSet('Api', 'Worker', 'Web')]
    [string]$Service = 'Api',
    [string]$Username = 'admin',
    [switch]$Browser
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令执行失败：$Program，退出码 $LASTEXITCODE" }
}

Push-Location $projectRoot
try {
    if ($Action -eq 'Init') {
        $configPath = Join-Path $projectRoot '.env.python'
        if (Test-Path -LiteralPath $configPath) { throw '.env.python 已存在，保留现有配置。' }
        $bytes = New-Object byte[] 24
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
        $secret = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
        $template = [IO.File]::ReadAllText((Join-Path $projectRoot '.env.example'))
        $template = $template.Replace('CHANGE_ME', $secret)
        # uv 的 --project 不应影响文件根；用绝对路径避免 API 与 Worker 工作目录差异。
        $uploadPath = (Join-Path $projectRoot 'data/python-uploads').Replace('\', '/')
        $template = $template.Replace('./data/python-uploads', $uploadPath)
        [IO.File]::WriteAllText($configPath, $template, (New-Object Text.UTF8Encoding $false))
        Write-Host '已创建 .env.python 和随机数据库密码；填写新环境连接及模型配置。'
        return
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw '未找到 uv。' }
    if ($Action -eq 'Setup') {
        $taskPython = Join-Path $projectRoot 'rewrite/backend/.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $taskPython)) { $taskPython = '3.12' }
        Invoke-Checked 'uv' @('sync', '--locked', '--project', 'rewrite/backend', '--python', $taskPython, '--cache-dir', 'data/rewrite-uv-cache')
        Invoke-Checked 'npm.cmd' @('--prefix', 'rewrite/frontend', 'ci', '--no-audit', '--no-fund')
        return
    }
    if ($Action -eq 'Verify') {
        & (Join-Path $projectRoot 'rewrite/scripts/check.ps1') -Browser:$Browser
        return
    }
    if (-not (Test-Path -LiteralPath '.env.python')) { throw '缺少 .env.python，请先运行 -Action Init。' }
    $runArgs = @('run', '--locked', '--env-file', '.env.python', '--project', 'rewrite/backend')
    switch ($Action) {
        'Check' { Invoke-Checked 'uv' ($runArgs + @('python', 'tools/check_runtime.py')) }
        'Migrate' { Invoke-Checked 'uv' ($runArgs + @('python', '-m', 'helpdesk.entrypoints.migrate', '--directory', 'rewrite/db/migrations')) }
        'Bootstrap' { Invoke-Checked 'uv' ($runArgs + @('python', '-m', 'helpdesk.entrypoints.admin', $Username)) }
        'Run' {
            switch ($Service) {
                'Api' { Invoke-Checked 'uv' ($runArgs + @('python', '-m', 'helpdesk.entrypoints.api')) }
                'Worker' { Invoke-Checked 'uv' ($runArgs + @('python', '-m', 'helpdesk.entrypoints.worker')) }
                'Web' { Invoke-Checked 'uv' ($runArgs + @('npm.cmd', '--prefix', 'rewrite/frontend', 'run', 'dev')) }
            }
        }
    }
} finally { Pop-Location }
