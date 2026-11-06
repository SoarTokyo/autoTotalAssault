# SPDX-License-Identifier: GPL-3.0-or-later
param(
    [string]$PythonExe,
    [string]$InstallerPath,
    [switch]$WithGui,
    [switch]$Offline,
    [switch]$RunTests
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonHome = Join-Path $projectRoot '.tools\Python311'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Assert-NativeSuccess([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Test-ProjectPython([string]$Candidate) {
    try {
        & $Candidate -c 'import sys; assert sys.version_info >= (3, 11) and sys.maxsize > 2**32' 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

try {
    if (-not $PythonExe -and (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe'))) {
        $PythonExe = Join-Path $pythonHome 'python.exe'
    }
    if (-not $PythonExe) {
        $candidate = Get-Command python -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.Source -notmatch 'WindowsApps' -and (Test-ProjectPython $candidate.Source)) { $PythonExe = $candidate.Source }
    }
    if (-not $PythonExe) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) {
            try {
                $detected = & $launcher.Source -3.11 -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $candidatePath = ($detected | Select-Object -Last 1).Trim()
                    if (Test-ProjectPython $candidatePath) { $PythonExe = $candidatePath }
                }
            } catch {
                Write-Host '系统 Python 启动器未找到可用的 Python 3.11，将配置项目专用环境。'
            }
        }
    }
    if (-not $PythonExe) {
        if (-not $InstallerPath) {
            if ($Offline) { throw '离线安装需要 -PythonExe 或 -InstallerPath。' }
            New-Item -ItemType Directory -Force -Path (Join-Path $projectRoot '.tools') | Out-Null
            $InstallerPath = Join-Path $projectRoot '.tools\python-3.11.9-amd64.exe'
            Write-Host '正在从 Python 官方网站下载完整安装程序……'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe' -OutFile $InstallerPath
        }
        $InstallerPath = (Resolve-Path -LiteralPath $InstallerPath).Path
        $signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw '安装文件未通过 Python Software Foundation 数字签名检查，已停止。'
        }
        Write-Host '正在安装项目专用 Python，不修改系统 PATH……'
        $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="{0}"' -f $pythonHome),
            'Include_pip=1', 'Include_launcher=0', 'Include_test=0', 'Include_doc=0',
            'Include_tcltk=0', 'AssociateFiles=0', 'Shortcuts=0', 'PrependPath=0')
        $process = Start-Process -FilePath $InstallerPath -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python 安装失败，退出码：$($process.ExitCode)" }
        $PythonExe = Join-Path $pythonHome 'python.exe'
    }
    & $PythonExe -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"; assert sys.maxsize > 2**32, "64-bit Python required"; print(sys.version)'
    Assert-NativeSuccess '需要 64 位 Python 3.11 或更高版本。'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & $PythonExe -m venv (Join-Path $projectRoot '.venv')
        Assert-NativeSuccess '虚拟环境创建失败。'
    }
    $extras = if ($WithGui) { 'gui,video' } else { 'video' }
    $runtimeLock = Join-Path $projectRoot 'requirements-win64.lock.txt'
    if (-not (Test-Path -LiteralPath $runtimeLock)) { throw '运行包缺少 requirements-win64.lock.txt，请完整解压程序。' }
    $pipArgs = @('-m', 'pip', 'install', '--only-binary=:all:', '-r', $runtimeLock,
        '-c', $runtimeLock, '-e', ($projectRoot + '[' + $extras + ']'))
    if ($Offline) { $pipArgs += @('--no-index', '--find-links', (Join-Path $projectRoot 'wheelhouse')) }
    & $venvPython @pipArgs
    Assert-NativeSuccess '依赖安装失败，请检查网络或离线 wheelhouse。'
    & $venvPython -m pip check
    Assert-NativeSuccess '依赖兼容性检查失败。'
    if ($RunTests) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'tests'))) { throw '本运行包不包含开发测试，请在完整开发仓库运行 -RunTests。' }
        & $venvPython -m unittest discover -s (Join-Path $projectRoot 'tests') -v
        Assert-NativeSuccess '离线测试未通过。'
    }
    if ($WithGui) {
        & $venvPython (Join-Path $PSScriptRoot 'check_environment.py')
        Assert-NativeSuccess 'GUI 运行依赖校验未通过。'
    }
    & $venvPython -m ba_recorder doctor
    Assert-NativeSuccess '环境检查未通过。'
    Write-Host '项目运行环境配置完成。'
} catch {
    Write-Error ("环境配置未完成：" + $_.Exception.Message)
    exit 1
}
