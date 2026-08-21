param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "setup", "init", "admin-key", "link", "start", "stop", "restart", "logs", "status", "test", "update")]
    [string]$Action = "help"
)

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
Set-Location -LiteralPath $projectDir

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "未找到 Docker。请先安装 Docker Desktop，并确认 docker compose 可用。"
    }
    & docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose 不可用。请启动 Docker Desktop 后重试。"
    }
}

function Invoke-Compose {
    param([string[]]$ComposeArgs)
    & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 执行失败，退出码：$LASTEXITCODE"
    }
}

function Show-Help {
    Write-Host "门训机器人 Docker 管理工具" -ForegroundColor Cyan
    Write-Host ""
    Write-Host ".\manage.ps1 setup       构建镜像并生成配置"
    Write-Host ".\manage.ps1 init        初始化 Delta Chat 机器人账号"
    Write-Host ".\manage.ps1 admin-key   设置管理员密钥"
    Write-Host ".\manage.ps1 link        显示机器人邀请链接"
    Write-Host ".\manage.ps1 start       后台启动"
    Write-Host ".\manage.ps1 logs        查看实时日志"
    Write-Host ".\manage.ps1 status      查看容器状态"
    Write-Host ".\manage.ps1 restart     重启服务"
    Write-Host ".\manage.ps1 stop        停止服务"
    Write-Host ".\manage.ps1 test        在镜像内运行测试"
    Write-Host ".\manage.ps1 update      拉取代码、重建并启动"
}

if ($Action -eq "help") {
    Show-Help
    exit 0
}

Assert-Docker

switch ($Action) {
    "setup" {
        New-Item -ItemType Directory -Force -Path ".\runtime\config", ".\runtime\data" | Out-Null
        if (-not (Test-Path -LiteralPath ".\.env")) {
            Copy-Item -LiteralPath ".\.env.example" -Destination ".\.env"
        }
        Invoke-Compose @("build")
        Invoke-Compose @("run", "--rm", "menxun-bot", "--help")
        Write-Host "初始化完成。下一步编辑 runtime\config\sites.json。" -ForegroundColor Green
    }
    "init" {
        $address = Read-Host "机器人邮箱，或 DCACCOUNT URI"
        if ($address.StartsWith("DCACCOUNT:", [System.StringComparison]::OrdinalIgnoreCase)) {
            Invoke-Compose @("run", "--rm", "menxun-bot", "init", $address)
        } else {
            $securePassword = Read-Host "邮箱授权码或应用密码" -AsSecureString
            $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
            try {
                $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
                Invoke-Compose @("run", "--rm", "menxun-bot", "init", $address, $plainPassword)
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
                $plainPassword = $null
            }
        }
    }
    "admin-key" {
        Invoke-Compose @("run", "--rm", "--entrypoint", "python", "menxun-bot", "/app/set_admin_key.py", "--file", "/data/admin-key.json")
    }
    "link" { Invoke-Compose @("run", "--rm", "menxun-bot", "link") }
    "start" { Invoke-Compose @("up", "-d") }
    "stop" { Invoke-Compose @("down") }
    "restart" { Invoke-Compose @("restart", "menxun-bot") }
    "logs" { Invoke-Compose @("logs", "--follow", "--tail", "200", "menxun-bot") }
    "status" { Invoke-Compose @("ps") }
    "test" {
        Invoke-Compose @("build")
        Invoke-Compose @("run", "--rm", "--entrypoint", "python", "menxun-bot", "-m", "unittest", "discover", "-v", "-s", "/app", "-p", "test_menxun_bot.py")
    }
    "update" {
        & git pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "git pull 失败，请检查本地修改。" }
        Invoke-Compose @("up", "-d", "--build")
    }
}
