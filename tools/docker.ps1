param(
    [ValidateSet('Check', 'Start', 'Stop', 'Status', 'Logs')]
    [string]$Action = 'Start',
    [ValidateRange(1, 65535)]
    [int]$WebPort = 3000,
    [ValidateRange(1, 65535)]
    [int]$PostgresPort = 55533,
    [string]$BuildProxy = ''
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '未找到 Docker，请安装并启动 Docker Desktop，使用 Linux 容器。' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) { throw '缺少 .env，请按 README 初始化账号及模型配置。' }

# 补入桌面进程尚未继承的凭据，不输出或写入配置文件。
foreach ($keyName in @('DEEPSEEK_API_KEY', 'DASHSCOPE_API_KEY', 'QWEN_API_KEY', 'IT_HELPDESK_RAG_API_KEY')) {
    if (-not [Environment]::GetEnvironmentVariable($keyName, 'Process')) {
        $savedValue = [Environment]::GetEnvironmentVariable($keyName, 'User')
        if (-not $savedValue) { $savedValue = [Environment]::GetEnvironmentVariable($keyName, 'Machine') }
        if ($savedValue) { [Environment]::SetEnvironmentVariable($keyName, $savedValue, 'Process') }
    }
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose -p it-helpdesk-rag-docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose 执行失败，退出码 $LASTEXITCODE" }
}

$previousWebPort = $env:WEB_PORT
$previousPostgresPort = $env:POSTGRES_PORT
$previousRevision = $env:APP_REVISION
Push-Location $projectRoot
try {
    # 与原生 PostgreSQL 的 55433 端口分开，容器使用独立命名卷。
    $env:WEB_PORT = [string]$WebPort
    $env:POSTGRES_PORT = [string]$PostgresPort
    switch ($Action) {
        'Check' {
            $engineType = & docker info --format '{{.OSType}}'
            if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎不可用，请启动 Docker Desktop。' }
            if ($engineType.Trim() -ne 'linux') { throw '请将 Docker Desktop 切换到 Linux 容器。' }
            Invoke-Compose @('config', '--quiet')
            Write-Host 'Docker 引擎与 Compose 配置检查通过；启动需要 Linux 容器。'
        }
        'Start' {
            if (-not $env:APP_REVISION -and (Get-Command git -ErrorAction SilentlyContinue)) {
                $revision = & git rev-parse HEAD
                if ($LASTEXITCODE -ne 0) { throw '无法读取 Git 版本，请检查仓库或设置 APP_REVISION。' }
                $changes = & git status --porcelain
                if ($LASTEXITCODE -ne 0) { throw '无法检查 Git 工作区状态。' }
                $env:APP_REVISION = $revision.Trim() + $(if ($changes) { '-dirty' } else { '' })
            }
            Invoke-Compose @('config', '--quiet')
            if ($BuildProxy) {
                # 构建进程位于 Linux 容器内，代理地址需从容器可达。
                Invoke-Compose @('build', '--build-arg', "HTTP_PROXY=$BuildProxy", '--build-arg', "HTTPS_PROXY=$BuildProxy")
            } else { Invoke-Compose @('build') }
            Invoke-Compose @('up', '--no-build', '-d', '--wait', '--wait-timeout', '180')
            Invoke-Compose @('ps')
            Write-Host "访问 http://localhost:$WebPort；使用现有 .env 中的账号。"
        }
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
