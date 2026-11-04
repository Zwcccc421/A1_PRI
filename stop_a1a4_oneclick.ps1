[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('all', 'receiver', 'flow', 'dashboard', 'remote')]
    [string]$Component = 'all',
    [switch]$KeepRemoteServer,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSCommandPath
$logRoot = Join-Path $projectRoot 'runtime_logs'
$launcherLog = Join-Path $logRoot 'launcher_stop.log'
$sshPath = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$serverUserHost = 'tangt@219.223.204.104'
$keyPath = 'C:\Users\robot\.ssh\a1_server_ed25519'
$cameraPort = 18081
$receiverPort = 8001
$flowPort = 8004
$dashboardPort = 8080

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    if (Test-Path -LiteralPath $logRoot) {
        Add-Content -LiteralPath $launcherLog -Value $line -Encoding UTF8
    }
}

function Reset-LogFile([string]$Path) {
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    [System.IO.File]::WriteAllText($Path, [string]::Empty, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-HttpOk([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch { return $false }
}

function Get-Json([string]$Url) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 3 } catch { return $null }
}

function Test-CameraReady {
    $health = Get-Json "http://127.0.0.1:$cameraPort/health"
    return $null -ne $health -and $health.video_ok -eq $true -and $health.audio_ok -eq $true
}

function Get-ProcessRecords {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.Name -in @('python.exe', 'pythonw.exe', 'ssh.exe') })
    } catch {
        Write-Warning '无法读取 Windows 进程命令行。请用管理员 PowerShell 重试。'
        return @()
    }
}

function Stop-CommandProcesses([string]$Label, [string[]]$Patterns) {
    $records = Get-ProcessRecords
    $matches = @($records | Where-Object {
        $commandLine = $_.CommandLine
        $commandLine -and ($Patterns | Where-Object { $commandLine -match $_ })
    } | Sort-Object ProcessId -Unique)

    if ($matches.Count -eq 0) {
        Write-Log "[skip] 未找到 $Label"
        return
    }

    foreach ($record in $matches) {
        $description = "$Label PID=$($record.ProcessId)"
        if ($PSCmdlet.ShouldProcess($description, 'Stop-Process -Force')) {
            Write-Log "[stop] $description"
            Stop-Process -Id $record.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-Remote([string]$Command) {
    if (-not (Test-Path -LiteralPath $sshPath)) {
        throw "找不到 SSH：$sshPath"
    }
    if (-not (Test-Path -LiteralPath $keyPath)) {
        throw "找不到 SSH 私钥：$keyPath"
    }
    & $sshPath `
        '-o' 'BatchMode=yes' `
        '-o' 'ConnectTimeout=5' `
        '-o' 'IdentitiesOnly=yes' `
        '-i' $keyPath `
        $serverUserHost `
        $Command 2>$null
}

function Test-RemoteHttp([int]$Port, [string]$Path) {
    try {
        if (-not (Test-Path -LiteralPath $sshPath) -or -not (Test-Path -LiteralPath $keyPath)) { return $false }
        $command = "curl -fsS --max-time 3 http://127.0.0.1:$Port$Path >/dev/null"
        [void](Invoke-Remote $command)
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Show-Status {
    $status = [ordered]@{
        '视频接收（含摄像头）' = (Test-CameraReady -and (Test-HttpOk "http://127.0.0.1:$receiverPort/docs"))
        '任务流程服务' = Test-HttpOk "http://127.0.0.1:$flowPort/a1/status"
        '大屏服务' = Test-HttpOk "http://127.0.0.1:$dashboardPort/"
        '服务器端 A1' = Test-RemoteHttp 18080 '/latest.jpg'
    }
    Write-Host ''
    Write-Host '========== 当前组件状态 ==========' -ForegroundColor Cyan
    foreach ($name in $status.Keys) {
        $text = if ($status[$name]) { '运行中' } else { '未运行' }
        $color = if ($status[$name]) { 'Green' } else { 'DarkYellow' }
        Write-Host ("{0,-18} {1}" -f ($name + ':'), $text) -ForegroundColor $color
    }
    Write-Host '=================================' -ForegroundColor Cyan
}

function Stop-RemoteA1 {
    if ($KeepRemoteServer) {
        Write-Log '[skip] 保留服务器端 A1'
        return
    }
    $command = @'
set +e

kill_tree() {
    parent="$1"
    for child in $(pgrep -P "$parent" 2>/dev/null || true); do
        kill_tree "$child"
    done
    kill -TERM "$parent" 2>/dev/null || true
}

# 先按启动脚本和项目目录递归结束进程，避免只结束外层 bash。
roots=$(pgrep -f '[r]un_streaming_a1_full_20260624.sh' || true)
for p in $roots; do kill_tree "$p"; done
project_roots=$(pgrep -f '[a]1_full_pipeline_improved' || true)
for p in $project_roots; do kill_tree "$p"; done

# 如果子进程已经脱离父进程，按 A1 的 18080 监听端口补充结束。
listeners=$(lsof -t -iTCP:18080 -sTCP:LISTEN 2>/dev/null || true)
if [ -z "$listeners" ] && command -v ss >/dev/null 2>&1; then
    listeners=$(ss -ltnp 2>/dev/null | grep ':18080 ' | sed -n 's/.*pid=\([0-9]*\).*/\1/p' | sort -u)
fi
for p in $listeners; do
    kill -TERM "$p" 2>/dev/null || true
done
sleep 1
for p in $listeners; do
    kill -KILL "$p" 2>/dev/null || true
done
exit 0
'@
    if ($PSCmdlet.ShouldProcess('服务器端 A1', '停止远程启动脚本及其直接子进程')) {
        Write-Log '[stop] 服务器端 A1'
        [void](Invoke-Remote $command)
        Start-Sleep -Seconds 2
        if (Test-RemoteHttp 18080 '/latest.jpg') {
            Write-Warning '服务器端 A1 的 18080 接口仍然可访问，远程可能还有脱离启动脚本的进程。'
        } else {
            Write-Log '[stopped] 服务器端 A1 HTTP/视频服务已不可访问'
        }
    }
}

if ($Status) {
    Show-Status
    exit 0
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
Reset-LogFile $launcherLog

try {
switch ($Component) {
    'remote' {
        Stop-RemoteA1
        Stop-CommandProcesses 'SSH 反向隧道' @('ssh.exe.*-R[ ]+(127.0.0.1:)?18081:127.0.0.1:18081', 'ssh.exe.*-R[ ]+(127.0.0.1:)?18083:127.0.0.1:8004')
    }
    'receiver' {
        Stop-CommandProcesses 'A4 视频汇聚服务' @('stream_receiverV9.py')
        Stop-CommandProcesses '本机摄像头/麦克风' @('local_usb_camera_stream.py')
    }
    'flow' {
        Stop-CommandProcesses 'A4 任务流程服务' @('full_process0714.py')
    }
    'dashboard' {
        Stop-CommandProcesses 'A4 大屏服务' @('pc_b_v3.py')
    }
    'all' {
        Stop-RemoteA1
        Stop-CommandProcesses 'SSH 反向隧道' @('ssh.exe.*-R[ ]+(127.0.0.1:)?18081:127.0.0.1:18081', 'ssh.exe.*-R[ ]+(127.0.0.1:)?18083:127.0.0.1:8004')
        Stop-CommandProcesses 'A4 大屏服务' @('pc_b_v3.py')
        Stop-CommandProcesses 'A4 任务流程服务' @('full_process0714.py')
        Stop-CommandProcesses 'A4 视频汇聚服务' @('stream_receiverV9.py')
        Stop-CommandProcesses '本机摄像头/麦克风' @('local_usb_camera_stream.py')
    }
}
} finally {
    Show-Status
}

Write-Log "[done] 已处理组件：$Component"
