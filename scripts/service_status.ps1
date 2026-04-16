<#
.SYNOPSIS
    QFE API Server 서비스 상태를 상세 조회합니다.
    sc query / sc qfailure / 이벤트 로그 / crash 카운터를 한 번에 출력합니다.

.PARAMETER ServiceName
    조회할 서비스 이름 (기본: qfe-api-server)

.PARAMETER LogDir
    로그 파일 경로 (기본: C:\QFE\logs)

.PARAMETER ShowRecentCrashes
    최근 crash 로그 파일 목록 표시 (기본: $true)
#>

param(
    [string]$ServiceName      = "qfe-api-server",
    [string]$LogDir           = "C:\QFE\logs",
    [bool]  $ShowRecentCrashes = $true
)

$ErrorActionPreference = "Continue"

Write-Host "============================================================"
Write-Host "  QFE API Server — 서비스 상태 조회"
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "============================================================"

# ── 1. 서비스 기본 상태 ───────────────────────────────────────────────────────
Write-Host "`n[ 서비스 상태 (sc query) ]"
$scOut = & sc.exe query $ServiceName 2>&1
Write-Host ($scOut -join "`n")

# Get-Service 로 추가 정보
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "`n  표시 이름 : $($svc.DisplayName)"
    Write-Host "  현재 상태 : $($svc.Status)"
    Write-Host "  시작 유형 : $($svc.StartType)"
}

# ── 2. 서비스 실패 설정 ───────────────────────────────────────────────────────
Write-Host "`n[ 실패 설정 (sc qfailure) ]"
$qfailOut = & sc.exe qfailure $ServiceName 2>&1
Write-Host ($qfailOut -join "`n")

# ── 3. Crash 카운터 ──────────────────────────────────────────────────────────
$counterFile = Join-Path $LogDir ".crash_counter"
Write-Host "`n[ Crash 카운터 ]"
if (Test-Path $counterFile) {
    $count = Get-Content $counterFile -Raw
    Write-Host "  총 크래시 횟수: $count 회"
} else {
    Write-Host "  크래시 기록 없음"
}

# ── 4. 최근 crash 로그 목록 ──────────────────────────────────────────────────
if ($ShowRecentCrashes -and (Test-Path $LogDir)) {
    Write-Host "`n[ 최근 Crash 로그 (최대 5개) ]"
    $crashLogs = Get-ChildItem -Path $LogDir -Filter "crash_*.log" |
        Sort-Object LastWriteTime -Descending | Select-Object -First 5
    if ($crashLogs) {
        foreach ($f in $crashLogs) {
            Write-Host "  $($f.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))  $($f.Name)"
        }
    } else {
        Write-Host "  없음"
    }
}

# ── 5. 서버 Health Check ─────────────────────────────────────────────────────
Write-Host "`n[ Health Check ]"
$baseUrl = "http://192.168.150.156:8080"
try {
    $cfgPath = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "config\config.yaml"
    if (Test-Path $cfgPath) {
        $cfgContent = Get-Content $cfgPath -Raw
        if ($cfgContent -match 'base_url\s*:\s*[''"]?([^''"#\r\n]+)[''"]?') {
            $baseUrl = $Matches[1].Trim()
        }
    }
} catch {}

try {
    $resp = Invoke-WebRequest -Uri "$baseUrl/health" -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
    Write-Host "  ✅ 서버 응답: HTTP $($resp.StatusCode) ($baseUrl/health)"
} catch {
    Write-Host "  ❌ 서버 응답 없음: $baseUrl/health"
    Write-Host "     $_"
}

# ── 6. 최근 이벤트 로그 ──────────────────────────────────────────────────────
Write-Host "`n[ 최근 Windows 이벤트 (Application, 오류/경고, 최근 10개) ]"
try {
    $events = Get-EventLog -LogName Application -Newest 200 -EntryType Error, Warning -ErrorAction Stop |
        Where-Object { $_.Source -match "QFE|qfe" } |
        Select-Object -First 10
    if ($events) {
        foreach ($e in $events) {
            Write-Host "  [$($e.TimeGenerated.ToString('MM-dd HH:mm'))] [$($e.EntryType)] EventID=$($e.EventID)"
            Write-Host "    $($e.Message.Split("`n")[0].Trim())"
        }
    } else {
        Write-Host "  (QFE 관련 이벤트 없음)"
    }
} catch {
    Write-Host "  이벤트 로그 조회 실패: $_"
}

Write-Host "`n============================================================"
