[CmdletBinding()]
param(
    [ValidateSet('all', 'receiver', 'flow', 'dashboard', 'remote')]
    [string]$Component = 'all',
    # -1 means try camera indexes 0, 1, 2, 3 in order.
    # Pass -CameraIndex 0 or -CameraIndex 1 when the camera is known.
    [int]$CameraIndex = -1,
    [switch]$SkipRemoteServer,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSCommandPath
$visualRoot = Join-Path $projectRoot '可视化0610'
$cameraRoot = Join-Path $projectRoot 'camera_computer_package'
$pythonPath = 'C:\Users\robot\miniconda3\envs\a1\python.exe'
$sshPath = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$serverUserHost = 'tangt@219.223.204.104'
$keyPath = 'C:\Users\robot\.ssh\a1_server_ed25519'
$remoteWorkDir = '/data0/tangt/a1_system/a1_full_pipeline_improved'
$remoteScript = 'run_streaming_a1_full_20260624.sh'
$logRoot = Join-Path $projectRoot 'runtime_logs'
$launcherLog = Join-Path $logRoot 'launcher_start.log'

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

function Get-CommandProcesses([string[]]$Patterns) {
    try {
        $records = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.Name -in @('python.exe', 'pythonw.exe', 'ssh.exe') }
        return @($records | Where-Object {
            $commandLine = $_.CommandLine
            $commandLine -and ($Patterns | Where-Object { $commandLine -match $_ })
        } | Sort-Object ProcessId -Unique)
    } catch {
        # 普通用户权限可能无法读取其他进程的命令行；端口/健康检查仍然可以继续保护重复启动。
        return @()
    }
}

function Format-ProcessIds($Processes) {
    return (($Processes | ForEach-Object { $_.ProcessId }) -join ', ')
}

function Test-TcpPort([int]$Port) {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $client.Connect('127.0.0.1', $Port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Test-HttpOk([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Get-Json([string]$Url) {
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec 3
    } catch {
        return $null
    }
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSec, [string]$Name) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) {
            Write-Log "[ready] $Name"
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Log "[timeout] $Name"
    return $false
}

function Start-LoggedProcess(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory,
    [string]$StdOutPath,
    [string]$StdErrPath
) {
    Write-Log "[start] $Name"
    # 每次真正启动新进程前清空对应日志，保留当前这次启动的输出。
    Reset-LogFile $StdOutPath
    Reset-LogFile $StdErrPath
    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $StdOutPath `
        -RedirectStandardError $StdErrPath `
        -WindowStyle Hidden `
        -PassThru
    Write-Log "[pid] $Name PID=$($process.Id)"
    return $process
}

function Test-CameraReady {
    $health = Get-Json "http://127.0.0.1:$cameraPort/health"
    return $null -ne $health -and $health.video_ok -eq $true -and $health.audio_ok -eq $true
}

function Start-CameraService {
    if (Test-CameraReady) {
        $existing = @(Get-CommandProcesses @('local_usb_camera_stream\.py'))
        if ($existing.Count -gt 0) {
            Write-Log "[duplicate] 本机摄像头服务已运行（PID=$((Format-ProcessIds $existing))），跳过启动"
        } else {
            Write-Log "[skip] 本机摄像头服务已经可用，沿用现有服务"
        }
        return
    }

    $existing = @(Get-CommandProcesses @('local_usb_camera_stream\.py'))
    if ($existing.Count -gt 0) {
        throw "检测到本机摄像头服务进程已存在（PID=$((Format-ProcessIds $existing))），但健康检查未通过。本次不再重复启动，请先检查或关闭旧进程。"
    }

    if (Test-TcpPort $cameraPort) {
        throw "本机端口 $cameraPort 已被占用，但摄像头健康检查未通过。请先关闭旧的摄像头进程。"
    }

    $candidates = if ($CameraIndex -ge 0) { @($CameraIndex) } else { @(0, 1, 2, 3) }
    foreach ($index in $candidates) {
        $stdout = Join-Path $logRoot ("camera_index_{0}.out.log" -f $index)
        $stderr = Join-Path $logRoot ("camera_index_{0}.err.log" -f $index)
        $args = @(
            '-u',
            (Join-Path $cameraRoot 'local_usb_camera_stream.py'),
            '--index', [string]$index,
            '--backend', 'dshow',
            '--host', '127.0.0.1',
            '--port', [string]$cameraPort,
            '--enable-audio',
            '--audio-device', '2',
            '--verbose'
        )
        $process = Start-LoggedProcess "本机摄像头/麦克风（index=$index）" $pythonPath $args $cameraRoot $stdout $stderr
        $ready = Wait-Until { Test-CameraReady } 20 "摄像头 index=$index"
        if ($ready) {
            Write-Log "[camera] 已选择摄像头 index=$index"
            return
        }

        if (-not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        if (Test-TcpPort $cameraPort) {
            throw "摄像头 index=$index 启动失败后仍占用端口 $cameraPort，请检查日志：$stderr"
        }
        Write-Log "[camera] index=$index 不可用，继续尝试下一个编号"
    }

    throw "没有找到可用的摄像头。请检查摄像头连接，或使用 -CameraIndex 0/1 指定编号。"
}

function Start-LocalService(
    [string]$Name,
    [int]$Port,
    [string[]]$Arguments,
    [string]$ReadyUrl,
    [int]$TimeoutSec = 30
) {
    $scriptName = @($Arguments | Where-Object { $_ -match '\.py$' } | Select-Object -First 1)
    $existing = if ($scriptName.Count -gt 0) {
        @(Get-CommandProcesses @([regex]::Escape([string]$scriptName[0])))
    } else { @() }

    if ($existing.Count -gt 0) {
        if (Test-HttpOk $ReadyUrl) {
            Write-Log "[duplicate] $Name 已运行（PID=$((Format-ProcessIds $existing))），跳过启动"
            return
        }
        throw "检测到 $Name 进程已存在（PID=$((Format-ProcessIds $existing))），但健康检查未通过。本次不再重复启动，请先检查或关闭旧进程。"
    }

    if (Test-HttpOk $ReadyUrl) {
        Write-Log "[skip] $Name 已经可用：$ReadyUrl"
        return
    }
    if (Test-TcpPort $Port) {
        throw "$Name 的端口 $Port 已被占用，但健康检查未通过。请先关闭旧进程。"
    }

    $scriptBase = if ($scriptName.Count -gt 0) {
        [System.IO.Path]::GetFileNameWithoutExtension([string]$scriptName[0])
    } else { 'service' }
    $safeName = switch ($scriptBase) {
        'stream_receiverV9' { 'receiver' }
        'full_process0714' { 'flow' }
        'pc_b_v3' { 'dashboard' }
        default { $scriptBase -replace '[^0-9A-Za-z_-]', '_' }
    }
    $stdout = Join-Path $logRoot "$safeName.out.log"
    $stderr = Join-Path $logRoot "$safeName.err.log"
    [void](Start-LoggedProcess $Name $pythonPath $Arguments $visualRoot $stdout $stderr)

    if (-not (Wait-Until { Test-HttpOk $ReadyUrl } $TimeoutSec $Name)) {
        throw "$Name 启动失败。请查看日志：$stderr"
    }
}

function Invoke-Remote([string]$Command) {
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
        $command = "curl -fsS --max-time 3 http://127.0.0.1:$Port$Path >/dev/null"
        [void](Invoke-Remote $command)
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Start-ReverseTunnel([int]$RemotePort, [int]$LocalPort, [string]$CheckPath) {
    $tunnelPattern = "ssh\.exe.*-R\s+(127\.0\.0\.1:)?${RemotePort}:127\.0\.0\.1:${LocalPort}"
    $existing = @(Get-CommandProcesses @($tunnelPattern))
    if ($existing.Count -gt 0) {
        if (Test-RemoteHttp $RemotePort $CheckPath) {
            Write-Log "[duplicate] SSH 隧道 $RemotePort -> $LocalPort 已运行（PID=$((Format-ProcessIds $existing))），跳过启动"
            return
        }
        throw "检测到 SSH 隧道 $RemotePort -> $LocalPort 已存在（PID=$((Format-ProcessIds $existing))），但健康检查未通过。本次不再重复启动。"
    }

    if (Test-RemoteHttp $RemotePort $CheckPath) {
        Write-Log "[skip] SSH 隧道 server:$RemotePort -> local:$LocalPort 已可用"
        return
    }

    $stdout = Join-Path $logRoot "ssh_reverse_$RemotePort.out.log"
    $stderr = Join-Path $logRoot "ssh_reverse_$RemotePort.err.log"
    $args = @(
        '-N',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'IdentitiesOnly=yes',
        '-i', $keyPath,
        '-R', ("{0}:127.0.0.1:{1}" -f $RemotePort, $LocalPort),
        $serverUserHost
    )
    [void](Start-LoggedProcess "SSH反向隧道 $RemotePort -> $LocalPort" $sshPath $args $projectRoot $stdout $stderr)

    if (-not (Wait-Until { Test-RemoteHttp $RemotePort $CheckPath } 20 "SSH隧道 $RemotePort")) {
        throw "SSH 反向隧道 $RemotePort 建立失败。请查看日志：$stderr"
    }
}

function Get-RemoteA1Matches {
    try {
        $output = Invoke-Remote "pgrep -af '[r]un_streaming_a1_full_20260624.sh'"
        if ($LASTEXITCODE -eq 0) { return @($output | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }) }
    } catch { }
    return @()
}

function Start-RemoteA1 {
    $existing = @(Get-RemoteA1Matches)
    if ($existing.Count -gt 0) {
        Write-Log "[duplicate] 服务器端 A1 已经在运行，跳过启动：$($existing -join ' | ')"
        return
    }

    $stdout = Join-Path $logRoot 'a1_server.out.log'
    $stderr = Join-Path $logRoot 'a1_server.err.log'
    $remoteCommand = "cd $remoteWorkDir && bash $remoteScript"
    $args = @(
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'IdentitiesOnly=yes',
        '-i', $keyPath,
        $serverUserHost,
        $remoteCommand
    )
    [void](Start-LoggedProcess '服务器端 A1' $sshPath $args $projectRoot $stdout $stderr)

    if (-not (Wait-Until { Test-RemoteHttp 18080 '/latest.jpg' } 90 '服务器端 A1 HTTP/视频服务')) {
        throw "服务器端 A1 启动失败。请查看日志：$stderr"
    }
}

function Get-ComponentStatus {
    [pscustomobject]@{
        receiver = (Test-CameraReady -and (Test-HttpOk "http://127.0.0.1:$receiverPort/docs"))
        flow = Test-HttpOk "http://127.0.0.1:$flowPort/a1/status"
        dashboard = Test-HttpOk "http://127.0.0.1:$dashboardPort/"
        remote = Test-RemoteHttp 18080 '/latest.jpg'
    }
}

function Show-Status {
    $status = Get-ComponentStatus
    Write-Host ''
    Write-Host '========== 当前组件状态 ==========' -ForegroundColor Cyan
    $labels = [ordered]@{
        receiver = '视频接收（含摄像头）'
        flow = '任务流程服务'
        dashboard = '大屏服务'
        remote = '服务器端 A1'
    }
    foreach ($name in $labels.Keys) {
        $text = if ($status.$name) { '运行中' } else { '未运行' }
        $color = if ($status.$name) { 'Green' } else { 'DarkYellow' }
        Write-Host ("{0,-18} {1}" -f ($labels[$name] + ':'), $text) -ForegroundColor $color
    }
    Write-Host '=================================' -ForegroundColor Cyan
}

function Start-ReceiverComponent {
    Start-CameraService
    Start-LocalService 'A4视频汇聚服务' $receiverPort @('-u', 'stream_receiverV9.py') "http://127.0.0.1:$receiverPort/docs"
}

function Start-FlowComponent {
    Start-LocalService 'A4任务流程服务' $flowPort @('-X', 'utf8', '-u', 'full_process0714.py') "http://127.0.0.1:$flowPort/a1/status"
}

function Start-DashboardComponent {
    Start-LocalService 'A4大屏服务' $dashboardPort @('-u', 'pc_b_v3.py') "http://127.0.0.1:$dashboardPort/"
}

function Start-RemoteComponent {
    Start-ReverseTunnel 18081 18081 '/health'
    Start-ReverseTunnel 18083 8004 '/a1/status'
    if (-not $SkipRemoteServer) {
        Start-RemoteA1
    } else {
        Write-Log '[skip] 根据参数跳过服务器端 A1 启动'
    }
}

if ($Status) {
    Show-Status
    exit 0
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到本机 Python 环境：$pythonPath"
}
if (-not (Test-Path -LiteralPath $visualRoot)) {
    throw "找不到可视化目录：$visualRoot"
}
if (-not (Test-Path -LiteralPath $cameraRoot)) {
    throw "找不到摄像头服务目录：$cameraRoot"
}
if (-not (Test-Path -LiteralPath $sshPath)) {
    throw "找不到 Windows OpenSSH：$sshPath"
}
if (-not (Test-Path -LiteralPath $keyPath)) {
    throw "找不到 SSH 私钥：$keyPath"
}

New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
Reset-LogFile $launcherLog

Write-Log '========== A1A4 一键启动开始 =========='
try {
    switch ($Component) {
        'receiver' { Start-ReceiverComponent }
        'flow' { Start-FlowComponent }
        'dashboard' { Start-DashboardComponent }
        'remote' { Start-RemoteComponent }
        'all' {
            Start-ReceiverComponent
            Start-FlowComponent
            Start-DashboardComponent
            Start-RemoteComponent
        }
    }
} finally {
    Show-Status
}

Write-Log "========== A1A4 启动完成：$Component =========="
Write-Log '大屏地址：http://127.0.0.1:8080/'
Write-Log "运行日志目录：$logRoot"
