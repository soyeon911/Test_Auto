<#
.SYNOPSIS
    QFE API 서버를 Windows Service로 등록하고 sc failure 자동 재시작을 설정합니다.

.DESCRIPTION
    동작 순서:
      1. NSSM 존재 확인 (없으면 자동 다운로드)
      2. 서비스 등록 (qfe-api-server)
      3. 환경변수·작업디렉터리·stdout/stderr 로그 설정
      4. sc failure — 1·2차 재시작 + 3차 reboot + 실패 시 on_service_failure.ps1 실행
      5. Windows 이벤트 소스 등록 (이벤트 뷰어에서 확인 가능)

.PARAMETER ServerDir
    qfe-server.exe 가 있는 디렉터리 (기본: 스크립트 상위 폴더\qfe-api-server-no-license)

.PARAMETER ServiceName
    등록할 서비스 이름 (기본: qfe-api-server)

.PARAMETER LogDir
    로그 파일 저장 경로 (기본: C:\QFE\logs)

.PARAMETER NssmDir
    NSSM 실행 파일 경로 (기본: C:\tools\nssm)

.EXAMPLE
    # 관리자 권한 PowerShell
    .\service_install.ps1
    .\service_install.ps1 -ServiceName "qfe-api" -LogDir "D:\Logs\QFE"
#>

#Requires -RunAsAdministrator

param(
    [string]$ServerDir   = "",
    [string]$ServiceName = "qfe-api-server",
    [string]$LogDir      = "C:\QFE\logs",
    [string]$NssmDir     = "C:\tools\nssm"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ── 경로 자동 해결 ────────────────────────────────────────────────────────────
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptRoot

if (-not $ServerDir) {
    $ServerDir = Join-Path $ProjectRoot "qfe-api-server-no-license"
}

$ServerExe    = Join-Path $ServerDir "qfe-server.exe"
$FailureScript = Join-Path $ScriptRoot "on_service_failure.ps1"
$NssmExe      = Join-Path $NssmDir "nssm.exe"

Write-Host "============================================================"
Write-Host "  QFE API Server — Windows Service 설치"
Write-Host "============================================================"
Write-Host "  서비스명  : $ServiceName"
Write-Host "  서버 경로 : $ServerExe"
Write-Host "  로그 경로 : $LogDir"
Write-Host "  NSSM 경로 : $NssmExe"
Write-Host "============================================================"

# ── 서버 실행 파일 존재 확인 ─────────────────────────────────────────────────
if (-not (Test-Path $ServerExe)) {
    Write-Error "qfe-server.exe 를 찾을 수 없습니다: $ServerExe"
    exit 1
}

# ── NSSM 확인 / 자동 다운로드 ────────────────────────────────────────────────
function Ensure-Nssm {
    if (Test-Path $NssmExe) {
        Write-Host "[NSSM] 발견: $NssmExe"
        return
    }

    Write-Host "[NSSM] 설치되어 있지 않음 — 다운로드 중..."
    New-Item -ItemType Directory -Path $NssmDir -Force | Out-Null

    $nssmZip = Join-Path $env:TEMP "nssm.zip"
    $nssmUrl = "https://nssm.cc/release/nssm-2.24.zip"

    try {
        Invoke-WebRequest -Uri $nssmUrl -OutFile $nssmZip -UseBasicParsing
        Expand-Archive -Path $nssmZip -DestinationPath $env:TEMP -Force
        $extracted = Get-Item "$env:TEMP\nssm-*\win64\nssm.exe" | Select-Object -First 1
        Copy-Item $extracted.FullName $NssmExe -Force
        Write-Host "[NSSM] 설치 완료: $NssmExe"
    } catch {
        Write-Error "NSSM 다운로드 실패: $_`n수동으로 https://nssm.cc 에서 nssm.exe 를 $NssmDir 에 복사하세요."
        exit 1
    }
}

Ensure-Nssm

# ── 로그 디렉터리 생성 ────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Write-Host "[Setup] 로그 디렉터리: $LogDir"

# ── 기존 서비스 중지·제거 (재설치 시) ────────────────────────────────────────
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[Service] 기존 서비스 발견 — 중지 후 제거..."
    & $NssmExe stop $ServiceName confirm 2>$null
    Start-Sleep -Seconds 2
    & $NssmExe remove $ServiceName confirm
    Start-Sleep -Seconds 1
    Write-Host "[Service] 기존 서비스 제거 완료"
}

# ── NSSM 으로 서비스 등록 ─────────────────────────────────────────────────────
Write-Host "[Service] 서비스 등록 중..."
& $NssmExe install $ServiceName $ServerExe
if ($LASTEXITCODE -ne 0) { Write-Error "서비스 등록 실패"; exit 1 }

# 작업 디렉터리 (DLL 로딩을 위해 서버 폴더로 설정)
& $NssmExe set $ServiceName AppDirectory $ServerDir

# stdout / stderr 파일 로그
$stdoutLog = Join-Path $LogDir "qfe-server-stdout.log"
$stderrLog = Join-Path $LogDir "qfe-server-stderr.log"
& $NssmExe set $ServiceName AppStdout      $stdoutLog
& $NssmExe set $ServiceName AppStderr      $stderrLog
& $NssmExe set $ServiceName AppRotateFiles 1          # 로그 로테이션 활성화
& $NssmExe set $ServiceName AppRotateBytes 10485760   # 10MB 마다 로테이션

# 서비스 표시 이름·설명
& $NssmExe set $ServiceName DisplayName "QFE API Server"
& $NssmExe set $ServiceName Description "Suprema QFE Face Recognition API Server (AutoTC managed)"

# 서비스 시작 유형: 자동
& $NssmExe set $ServiceName Start SERVICE_AUTO_START

# 비정상 종료 코드 (0 은 정상 종료로 인식, 재시작 안 함)
& $NssmExe set $ServiceName AppExit Default Restart

Write-Host "[Service] NSSM 설정 완료"

# ── sc failure — 자동 재시작 정책 ────────────────────────────────────────────
# reset=300   : 300초(5분) 이내 재실패가 없으면 실패 카운터 초기화
# actions=    : 1차 restart/5000ms → 2차 restart/10000ms → 3차 reboot/30000ms
# command=    : 모든 실패 시 on_service_failure.ps1 실행 (로그 기록)
#
# ※ sc failure 의 command= 는 actions 와 독립적으로 항상 실행됩니다.
#   PowerShell 스크립트 경로에 공백이 있으면 따옴표로 감싸야 합니다.
# ─────────────────────────────────────────────────────────────────────────────
Write-Host "[sc failure] 자동 재시작 정책 설정 중..."

# failure action 명령: on_service_failure.ps1 호출
$failurePath = $FailureScript -replace '\\', '\\'  # 경로의 백슬래시 이스케이프
$failureCmd  = "powershell -NoProfile -ExecutionPolicy Bypass -File '$FailureScript' -ServiceName '$ServiceName' -LogDir '$LogDir'"

# sc failure 설정
sc.exe failure $ServiceName reset=300 actions=restart/5000/restart/10000/reboot/30000 command=$failureCmd

if ($LASTEXITCODE -ne 0) {
    Write-Warning "sc failure 설정 실패 (exit=$LASTEXITCODE). 수동으로 재설정이 필요할 수 있습니다."
} else {
    Write-Host "[sc failure] 설정 완료"
    Write-Host "  1차 실패: 5초 후 재시작"
    Write-Host "  2차 실패: 10초 후 재시작"
    Write-Host "  3차 실패: 30초 후 재부팅"
    Write-Host "  매 실패: on_service_failure.ps1 실행"
}

# 실패 시 actions 실행을 비정상 종료 코드에도 적용
sc.exe failureflag $ServiceName 1

# ── Windows 이벤트 소스 등록 ─────────────────────────────────────────────────
$eventSource = "QFEApiServer"
if (-not [System.Diagnostics.EventLog]::SourceExists($eventSource)) {
    New-EventLog -LogName Application -Source $eventSource
    Write-Host "[EventLog] 이벤트 소스 등록: $eventSource"
} else {
    Write-Host "[EventLog] 이벤트 소스 이미 존재: $eventSource"
}

# ── 서비스 시작 ────────────────────────────────────────────────────────────
Write-Host "[Service] 서비스 시작 중..."
& $NssmExe start $ServiceName
Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host ""
    Write-Host "✅ 서비스 시작 성공!"
    Write-Host "   상태  : $($svc.Status)"
    Write-Host "   로그  : $LogDir"
} else {
    Write-Warning "서비스 시작 확인 필요 (상태: $($svc?.Status)). 로그를 확인하세요: $stderrLog"
}

# ── 설치 요약 출력 ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================================"
Write-Host "  설치 완료 — 유용한 명령어"
Write-Host "============================================================"
Write-Host "  상태 조회  : sc query $ServiceName"
Write-Host "              또는: .\service_status.ps1"
Write-Host "  중지       : sc stop $ServiceName"
Write-Host "  시작       : sc start $ServiceName"
Write-Host "  재시작     : sc stop $ServiceName && sc start $ServiceName"
Write-Host "  서비스 제거: .\service_remove.ps1"
Write-Host "  표준 출력  : $stdoutLog"
Write-Host "  표준 에러  : $stderrLog"
Write-Host "============================================================"
