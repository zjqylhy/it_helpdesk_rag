param(
    [ValidateSet('Check', 'Build', 'Migrate', 'Bootstrap', 'Start', 'Stop', 'Status', 'Logs')]
    [string]$Action = 'Check',
    [ValidateRange(1, 65535)][int]$WebPort = 3000,
    [ValidateRange(1, 65535)][int]$PostgresPort = 55534,
    [string]$Username = 'admin'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '未找到 Docker。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env.python'))) { throw '先运行 tools/windows.ps1 -Action Init。' }
function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose --env-file .env.python -p it-helpdesk-python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose 失败，退出码 $LASTEXITCODE" }
}
$previousWebPort = $env:WEB_PORT
$previousPostgresPort = $env:POSTGRES_PORT
$previousRevision = $env:APP_REVISION
Push-Location $projectRoot
try {
    $env:WEB_PORT = [string]$WebPort
    $env:POSTGRES_PORT = [string]$PostgresPort
    if (-not $env:APP_REVISION) {
        $revision = & git rev-parse HEAD
        if ($LASTEXITCODE -ne 0) { throw '无法读取 Git 版本。' }
        $changes = & git status --porcelain
        if ($LASTEXITCODE -ne 0) { throw '无法检查 Git 状态。' }
        $env:APP_REVISION = $revision.Trim() + $(if ($changes) { '-dirty' } else { '' })
    }
    switch ($Action) {
        'Check' {
            $engineType = & docker info --format '{{.OSType}}'
            if ($LASTEXITCODE -ne 0 -or $engineType.Trim() -ne 'linux') { throw '请启动 Docker Desktop Linux 引擎。' }
            Invoke-Compose @('config', '--quiet')
        }
        'Build' { Invoke-Compose @('build', 'api', 'web') }
        'Migrate' {
            Invoke-Compose @('up', '-d', '--wait', 'db')
            # 显式迁移，不借启动 API 隐式改库；运行中的 API 会由数据库锁阻止迁移。
            Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'python', '-m', 'helpdesk.entrypoints.migrate', '--directory', '/workspace/rewrite/db/migrations')
        }
        'Bootstrap' {
            Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'python', '-m', 'helpdesk.entrypoints.admin', $Username)
        }
        'Start' { Invoke-Compose @('up', '--no-build', '-d', '--wait', '--wait-timeout', '180') }
        'Stop' { Invoke-Compose @('down') }
        'Status' { Invoke-Compose @('ps') }
        'Logs' { Invoke-Compose @('logs', '--tail', '100', '--follow') }
    }
} finally {
    $env:WEB_PORT = $previousWebPort
    $env:POSTGRES_PORT = $previousPostgresPort
    $env:APP_REVISION = $previousRevision
    Pop-Location
}
