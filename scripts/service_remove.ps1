<#
.SYNOPSIS
    QFE API Server Windows Service를 중지하고 제거합니다.

.PARAMETER ServiceName
    제거할 서비스 이름 (기본: qfe-api-server)

.PARAMETER NssmDir
    NSSM 실행 파일 경로 (기본: C:\tools\nssm)

.PARAMETER RemoveLogs
    $true 이면 로그 파일도 함께 삭제 (기본: $false)
#>

#Requires -RunAsAdministrator

param(
    [string]$ServiceName = "qfe-api-server",
    [string]$NssmDir     = "C:\tools\nssm",
    [bool]  $RemoveLogs  = $false,
    [string]$LogDir      = "C:\QFE\logs"
)

$NssmExe = Join-Path $NssmDir "nssm.exe"

Write-Host "[Remove] 서비스 제거: $ServiceName"

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Host "[Remove] 서비스가 존재하지 않습니다: $ServiceName"
    exit 0
}

# 서비스 중지
if ($svc.Status -eq "Running") {
    Write-Host "[Remove] 서비스 중지 중..."
    if (Test-Path $NssmExe) {
        & $NssmExe stop $ServiceName confirm
    } else {
        Stop-Service -Name $ServiceName -Force
    }
    Start-Sleep -Seconds 3
}

# NSSM 으로 제거
if (Test-Path $NssmExe) {
    & $NssmExe remove $ServiceName confirm
} else {
    # NSSM 없으면 sc.exe 로 직접 제거
    sc.exe delete $ServiceName
}

# Windows 이벤트 소스 제거 (선택)
try {
    if ([System.Diagnostics.EventLog]::SourceExists("QFEApiServer")) {
        Remove-EventLog -Source "QFEApiServer" -ErrorAction SilentlyContinue
        Write-Host "[Remove] 이벤트 소스 제거 완료"
    }
} catch {}

# 로그 삭제 (선택)
if ($RemoveLogs -and (Test-Path $LogDir)) {
    Remove-Item -Path $LogDir -Recurse -Force
    Write-Host "[Remove] 로그 디렉터리 삭제: $LogDir"
}

Write-Host "[Remove] 서비스 제거 완료: $ServiceName"
