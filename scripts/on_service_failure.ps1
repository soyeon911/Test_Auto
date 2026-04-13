<#
.SYNOPSIS
    서비스 실패(crash) 시 sc failure 의 command= 로 자동 호출되는 스크립트.
    crash 당시 상황을 기록하고 Windows 이벤트 로그에 남깁니다.

.DESCRIPTION
    기록 항목:
      - 타임스탬프
      - 서비스 상태 (sc query)
      - 프로세스 종료 코드 (이전 실행 정보)
      - Windows Application/System 이벤트 로그 (최근 오류)
      - 메모리·CPU 스냅샷 (tasklist, 시스템 리소스)
      - WER 크래시 덤프 경로
      - 재시작 카운터 (몇 번째 실패인지)

.PARAMETER ServiceName
    모니터링 중인 서비스 이름 (기본: qfe-api-server)

.PARAMETER LogDir
    로그 저장 경로 (기본: C:\QFE\logs)
#>

param(
    [string]$ServiceName = "qfe-api-server",
    [string]$LogDir      = "C:\QFE\logs"
)

$ErrorActionPreference = "Continue"

# ── 초기화 ────────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$timestamp  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$fileStamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile    = Join-Path $LogDir "crash_${fileStamp}.log"
$counterFile = Join-Path $LogDir ".crash_counter"
$eventSource = "QFEApiServer"

# ── 실패 카운터 ───────────────────────────────────────────────────────────────
$crashCount = 1
if (Test-Path $counterFile) {
    try { $crashCount = [int](Get-Content $counterFile -Raw) + 1 } catch {}
}
$crashCount | Out-File $counterFile -NoNewline -Encoding ascii

# ── 로그 헤더 ─────────────────────────────────────────────────────────────────
$report = @"
================================================================
  QFE API Server — Crash Report #$crashCount
  Timestamp : $timestamp
  Service   : $ServiceName
  Log File  : $logFile
================================================================

"@

# ── 1. 서비스 상태 (sc query) ─────────────────────────────────────────────────
$report += "[ 1. 서비스 상태 (sc query) ]`n"
try {
    $scOutput = & sc.exe query $ServiceName 2>&1
    $report += ($scOutput -join "`n") + "`n`n"
} catch {
    $report += "sc query 실패: $_`n`n"
}

# ── 2. 서비스 실패 설정 확인 (sc qfailure) ───────────────────────────────────
$report += "[ 2. 서비스 실패 설정 (sc qfailure) ]`n"
try {
    $qfailOutput = & sc.exe qfailure $ServiceName 2>&1
    $report += ($qfailOutput -join "`n") + "`n`n"
} catch {
    $report += "sc qfailure 실패: $_`n`n"
}

# ── 3. 프로세스 목록 (qfe-server 관련) ───────────────────────────────────────
$report += "[ 3. 관련 프로세스 (tasklist) ]`n"
try {
    $procs = Get-Process | Where-Object { $_.Name -like "*qfe*" -or $_.Name -like "*server*" } |
        Select-Object Name, Id, CPU, WorkingSet, StartTime |
        Format-Table -AutoSize | Out-String
    $report += if ($procs.Trim()) { $procs } else { "(실행 중인 관련 프로세스 없음)`n" }
    $report += "`n"
} catch {
    $report += "프로세스 조회 실패: $_`n`n"
}

# ── 4. 시스템 리소스 스냅샷 ──────────────────────────────────────────────────
$report += "[ 4. 시스템 리소스 ]`n"
try {
    $cpu     = (Get-CimInstance -ClassName Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $memInfo = Get-CimInstance -ClassName Win32_OperatingSystem
    $memUsed = [math]::Round(($memInfo.TotalVisibleMemorySize - $memInfo.FreePhysicalMemory) / 1MB, 1)
    $memTotal = [math]::Round($memInfo.TotalVisibleMemorySize / 1MB, 1)
    $report += "  CPU 사용률 : ${cpu}%`n"
    $report += "  메모리     : ${memUsed}GB / ${memTotal}GB`n"
    $report += "`n"
} catch {
    $report += "리소스 조회 실패: $_`n`n"
}

# ── 5. Windows Application 이벤트 로그 (최근 오류·경고) ──────────────────────
$report += "[ 5. Windows Application 이벤트 로그 (최근 20개 오류) ]`n"
try {
    $events = Get-EventLog -LogName Application -Newest 50 -EntryType Error, Warning -ErrorAction Stop |
        Where-Object { $_.Source -match "QFE|qfe|server" -or $_.EventID -in @(1000, 1001, 1002) } |
        Select-Object -First 20 |
        ForEach-Object { "[$($_.TimeGenerated)] [$($_.EntryType)] $($_.Source) (ID:$($_.EventID))`n  $($_.Message.Split("`n")[0])" }
    $report += if ($events) { ($events -join "`n") + "`n" } else { "(관련 이벤트 없음)`n" }
    $report += "`n"
} catch {
    $report += "이벤트 로그 조회 실패: $_`n`n"
}

# ── 6. WER 크래시 덤프 위치 확인 ─────────────────────────────────────────────
$report += "[ 6. 최근 크래시 덤프 ]`n"
$werPaths = @(
    "$env:LOCALAPPDATA\CrashDumps",
    "C:\Windows\Minidump",
    "$env:ProgramData\Microsoft\Windows\WER\ReportQueue"
)
$foundDumps = @()
foreach ($path in $werPaths) {
    if (Test-Path $path) {
        $dumps = Get-ChildItem -Path $path -Recurse -Include "*.dmp", "*.wer" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 3
        foreach ($d in $dumps) {
            $foundDumps += "  $($d.FullName) ($($d.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss')), $([math]::Round($d.Length/1MB,1))MB)"
        }
    }
}
$report += if ($foundDumps) { ($foundDumps -join "`n") + "`n" } else { "  (덤프 파일 없음)`n" }
$report += "`n"

# ── 7. 최근 stdout/stderr 로그 꼬리 ──────────────────────────────────────────
$report += "[ 7. 서버 로그 (최근 50줄) ]`n"
foreach ($logName in @("qfe-server-stderr.log", "qfe-server-stdout.log")) {
    $logPath = Join-Path $LogDir $logName
    if (Test-Path $logPath) {
        $lines = Get-Content $logPath -Tail 50 -ErrorAction SilentlyContinue
        $report += "-- $logName --`n"
        $report += if ($lines) { ($lines -join "`n") + "`n" } else { "(비어 있음)`n" }
        $report += "`n"
    }
}

# ── 로그 파일 저장 ────────────────────────────────────────────────────────────
$report | Out-File $logFile -Encoding utf8
Write-Host "[Failure] Crash 리포트 저장: $logFile"

# ── Windows 이벤트 로그에 기록 ───────────────────────────────────────────────
try {
    if (-not [System.Diagnostics.EventLog]::SourceExists($eventSource)) {
        New-EventLog -LogName Application -Source $eventSource -ErrorAction SilentlyContinue
    }

    $summary = "QFE API Server crash #$crashCount at $timestamp.`nLog: $logFile"
    Write-EventLog -LogName Application -Source $eventSource `
        -EventId 1001 -EntryType Error -Message $summary
    Write-Host "[Failure] Windows 이벤트 로그 기록 완료 (EventID 1001)"
} catch {
    Write-Host "[Failure] 이벤트 로그 기록 실패: $_"
}

# ── 알림 (선택: 이메일 또는 webhook) ─────────────────────────────────────────
# 아래 주석을 해제하고 설정하면 crash 시 슬랙/Teams 알림 가능
#
# $webhookUrl = $env:SLACK_WEBHOOK_URL
# if ($webhookUrl) {
#     $body = @{ text = "⚠️ QFE Server crash #$crashCount at $timestamp" } | ConvertTo-Json
#     Invoke-RestMethod -Uri $webhookUrl -Method Post -Body $body -ContentType "application/json" -ErrorAction SilentlyContinue
# }

Write-Host "[Failure] 처리 완료. 크래시 카운터: $crashCount"
