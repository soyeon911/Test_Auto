<#
.SYNOPSIS
    서버 crash 발생 시 관련 로그를 수집하는 스크립트

.DESCRIPTION
    다음 소스에서 로그를 수집합니다:
      - 프로젝트 내 ./logs 디렉터리
      - Windows 이벤트 로그 (Application, System)
      - QFE 서버 로그 경로 (config/config.yaml의 경로)
      - Windows 오류 보고 (WER) 크래시 덤프

.PARAMETER OutputDir
    수집된 로그를 저장할 디렉터리 경로

.PARAMETER Label
    로그 파일명 접두사 (pre_test / post_test 등)

.EXAMPLE
    .\collect_crash_logs.ps1 -OutputDir "crash_logs" -Label "post_test"
#>

param(
    [string]$OutputDir = "crash_logs",
    [string]$Label     = "crash"
)

$ErrorActionPreference = "Continue"

# ── 출력 디렉터리 준비 ─────────────────────────────────────────────────────────
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
Write-Host "[CrashLog] 수집 시작 → $OutputDir ($timestamp)"

# ── 1. 프로젝트 로그 파일 ────────────────────────────────────────────────────
$projectLogPatterns = @(
    "logs\*.log",
    "logs\*.txt",
    "reports\*.log"
)
foreach ($pattern in $projectLogPatterns) {
    $files = Get-Item $pattern -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $dest = Join-Path $OutputDir "$Label`_$($f.Name)"
        Copy-Item $f.FullName $dest -Force -ErrorAction SilentlyContinue
        Write-Host "[CrashLog] 복사: $($f.FullName) → $dest"
    }
}

# ── 2. config.yaml 에서 서버 로그 경로 읽기 ──────────────────────────────────
$configFile = "config\config.yaml"
if (Test-Path $configFile) {
    $content = Get-Content $configFile -Raw
    # log_path: "C:\path\to\logs" 패턴 추출
    if ($content -match 'log_path\s*:\s*[''"]?([^''"#\r\n]+)[''"]?') {
        $serverLogDir = $Matches[1].Trim()
        Write-Host "[CrashLog] config에서 서버 로그 경로: $serverLogDir"
        if (Test-Path $serverLogDir) {
            Get-ChildItem -Path $serverLogDir -Filter "*.log" -Recurse |
                ForEach-Object {
                    $dest = Join-Path $OutputDir "$Label`_server_$($_.Name)"
                    Copy-Item $_.FullName $dest -Force -ErrorAction SilentlyContinue
                    Write-Host "[CrashLog] 서버 로그: $($_.FullName)"
                }
        }
    }
}

# ── 3. QFE 서버 기본 로그 경로 ───────────────────────────────────────────────
$qfeLogPaths = @(
    "C:\QFE\logs",
    "C:\Program Files\Suprema\QFE\logs",
    "$env:ProgramData\QFE\logs",
    "$env:LOCALAPPDATA\QFE\logs"
)
foreach ($path in $qfeLogPaths) {
    if (Test-Path $path) {
        Write-Host "[CrashLog] QFE 로그 경로 발견: $path"
        Get-ChildItem -Path $path -Filter "*.log" -Recurse |
            Sort-Object LastWriteTime -Descending | Select-Object -First 10 |
            ForEach-Object {
                $dest = Join-Path $OutputDir "$Label`_qfe_$($_.Name)"
                Copy-Item $_.FullName $dest -Force -ErrorAction SilentlyContinue
                Write-Host "[CrashLog] QFE 로그: $($_.FullName)"
            }
        break
    }
}

# ── 4. Windows 이벤트 로그 (Application) ─────────────────────────────────────
try {
    $evtFile = Join-Path $OutputDir "$Label`_eventlog_application.txt"
    Get-EventLog -LogName Application -Newest 100 -EntryType Error, Warning |
        Select-Object TimeGenerated, EntryType, Source, EventID, Message |
        Format-List |
        Out-File $evtFile -Encoding utf8
    Write-Host "[CrashLog] Windows Application 이벤트 로그 저장: $evtFile"
} catch {
    Write-Host "[CrashLog] 이벤트 로그 읽기 실패: $_"
}

# ── 5. Windows 이벤트 로그 (System — 서비스 관련) ────────────────────────────
try {
    $sysFile = Join-Path $OutputDir "$Label`_eventlog_system.txt"
    Get-EventLog -LogName System -Newest 50 -EntryType Error, Warning |
        Where-Object { $_.Source -match "Service|SCM|Windows Error" } |
        Select-Object TimeGenerated, EntryType, Source, EventID, Message |
        Format-List |
        Out-File $sysFile -Encoding utf8
    Write-Host "[CrashLog] Windows System 이벤트 로그 저장: $sysFile"
} catch {
    Write-Host "[CrashLog] System 이벤트 로그 읽기 실패: $_"
}

# ── 6. Windows 오류 보고 (WER) 크래시 덤프 ───────────────────────────────────
$werPaths = @(
    "$env:LOCALAPPDATA\CrashDumps",
    "C:\Windows\Minidump",
    "$env:ProgramData\Microsoft\Windows\WER\ReportQueue"
)
foreach ($werPath in $werPaths) {
    if (Test-Path $werPath) {
        Get-ChildItem -Path $werPath -Recurse -Include "*.dmp","*.wer" |
            Sort-Object LastWriteTime -Descending | Select-Object -First 5 |
            ForEach-Object {
                # 덤프 파일은 용량이 클 수 있으므로 1GB 이하만 복사
                if ($_.Length -lt 1GB) {
                    $dest = Join-Path $OutputDir "$Label`_dump_$($_.Name)"
                    Copy-Item $_.FullName $dest -Force -ErrorAction SilentlyContinue
                    Write-Host "[CrashLog] 크래시 덤프: $($_.FullName) ($([math]::Round($_.Length/1MB,1))MB)"
                } else {
                    Write-Host "[CrashLog] 덤프 크기 초과, 스킵: $($_.FullName) ($([math]::Round($_.Length/1GB,1))GB)"
                }
            }
        break
    }
}

# ── 7. 수집 요약 ─────────────────────────────────────────────────────────────
$collected = (Get-ChildItem -Path $OutputDir).Count
Write-Host "[CrashLog] 수집 완료: $collected 개 파일 → $OutputDir"
