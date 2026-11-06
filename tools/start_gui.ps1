# SPDX-License-Identifier: GPL-3.0-or-later
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logRoot = Join-Path $projectRoot 'logs'
$transcribing = $false
$environmentLock = $null
$failed = $false
try {
    Set-Location -LiteralPath $projectRoot
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
    # 防止双击多次导致 pip 同时修改一个环境。
    $environmentLock = [System.IO.File]::Open((Join-Path $logRoot 'environment.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $logPath = Join-Path $logRoot ('environment-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    Start-Transcript -LiteralPath $logPath | Out-Null
    $transcribing = $true
    $env:PYTHONIOENCODING = 'utf-8'
    # 录制和分析子进程也必须读取当前目录的源码，避免移动目录后 editable 路径失效。
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    $usable = $false
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -c 'import sys, tomllib; assert sys.version_info >= (3, 11) and sys.maxsize > 2**32' 2>$null
        $usable = $LASTEXITCODE -eq 0
        if (-not $usable) {
            throw '现有 .venv 无法运行，可能从其他电脑复制或移动后失效。请关闭程序，将 .venv 重命名为 .venv.old 后重新双击启动；录制和技能轴无需删除。'
        }
    }
    if (-not $usable) {
        Write-Host '首次运行，正在创建项目 Python 环境并安装依赖……'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup.ps1') -WithGui
        if ($LASTEXITCODE -ne 0) { throw '首次安装失败，请查看上方信息。' }
    }
    & $venvPython (Join-Path $PSScriptRoot 'check_environment.py') --install
    if ($LASTEXITCODE -ne 0) { throw '启动校验没有通过，已停止启动界面。' }
    if (-not $CheckOnly) {
        Start-Process -FilePath (Join-Path $projectRoot '.venv\Scripts\pythonw.exe') -ArgumentList ('"{0}"' -f (Join-Path $PSScriptRoot 'launch_gui.py')) -WorkingDirectory $projectRoot -WindowStyle Hidden
    }
} catch {
    $failed = $true
    Write-Host ('无法启动：' + $_.Exception.Message) -ForegroundColor Red
    if ($logPath) { Write-Host ('诊断日志：' + $logPath) }
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($environmentLock) { $environmentLock.Dispose() }
}
if ($failed) {
    if (-not $CheckOnly) { Read-Host '按回车关闭窗口' | Out-Null }
    exit 1
}
