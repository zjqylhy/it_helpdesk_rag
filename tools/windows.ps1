param(
    [ValidateSet('Check', 'Init', 'Setup', 'Run')]
    [string]$Action = 'Check',
    [ValidateSet('Backend', 'Rag', 'Web')]
    [string]$Service = 'Backend'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$env:PYTHONUTF8 = '1'

function Invoke-ProjectCommand {
    param([string]$Directory, [string]$Program, [string[]]$Arguments)
    Push-Location $Directory
    try {
        & $Program @Arguments
        if ($LASTEXITCODE -ne 0) { throw "命令执行失败：$Program，退出码 $LASTEXITCODE" }
    } finally { Pop-Location }
}

function New-LocalSecret {
    $bytes = New-Object byte[] 24
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}

if ($Action -eq 'Init') {
    $configPath = Join-Path $projectRoot '.env'
    if (Test-Path -LiteralPath $configPath) { throw '.env 已存在，不会覆盖；请直接编辑现有文件。' }
    $template = [IO.File]::ReadAllText((Join-Path $projectRoot '.env.example'))
    $template = $template.Replace('INTERNAL_TOKEN=replace-with-a-long-random-string', "INTERNAL_TOKEN=$(New-LocalSecret)")
    $template = $template.Replace('ADMIN_PASSWORD=replace-with-a-strong-password', "ADMIN_PASSWORD=$(New-LocalSecret)")
    $template = $template.Replace('USER_PASSWORD=replace-with-another-password', "USER_PASSWORD=$(New-LocalSecret)")
    [IO.File]::WriteAllText($configPath, $template, (New-Object System.Text.UTF8Encoding $false))
    Write-Host '已创建 .env 并生成本地账号密码与内部令牌；填写数据库连接、模型 Key 和业务空间地址。'
    exit 0
}

if ($Action -eq 'Setup') {
    foreach ($program in @('go', 'uv', 'node', 'npm.cmd')) {
        if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { throw "未找到 $program" }
    }
    Invoke-ProjectCommand (Join-Path $projectRoot 'backend') 'go' @('mod', 'download')
    Invoke-ProjectCommand (Join-Path $projectRoot 'rag') 'uv' @('sync', '--frozen', '--extra', 'test', '--extra', 'faithfulness', '--cache-dir', '../data/uv-cache')
    Invoke-ProjectCommand (Join-Path $projectRoot 'frontend') 'npm.cmd' @('ci', '--cache', '../data/npm-cache')
    Write-Host '项目依赖安装完成。该命令不安装数据库，也不调用模型。'
    exit 0
}

if ($Action -eq 'Check') {
    $missing = $false
    foreach ($program in @('go', 'uv', 'node', 'npm.cmd')) {
        $command = Get-Command $program -ErrorAction SilentlyContinue
        if ($command) { Write-Host "$program : 已找到" } else { Write-Host "$program : 缺少"; $missing = $true }
    }
    $pythonPath = Join-Path $projectRoot 'rag/.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        Write-Host 'Python 项目环境：缺少，请运行 -Action Setup。'
        exit 1
    }
    & $pythonPath (Join-Path $PSScriptRoot 'windows_check.py')
    if ($LASTEXITCODE -ne 0 -or $missing) { exit 1 }
    exit 0
}

# 每次运行一个前台服务，Ctrl+C 即可停止；不创建系统服务或后台窗口。
switch ($Service) {
    'Backend' { Invoke-ProjectCommand (Join-Path $projectRoot 'backend') 'go' @('run', '.') }
    'Rag' { Invoke-ProjectCommand (Join-Path $projectRoot 'rag') 'uv' @('run', 'python', '-m', 'helpdesk') }
    'Web' { Invoke-ProjectCommand (Join-Path $projectRoot 'frontend') 'npm.cmd' @('run', 'dev', '--', '--port', '5173', '--strictPort') }
}
