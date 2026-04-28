param(
  [Parameter(Mandatory=$true)][string]$ServerExe,
  [Parameter(Mandatory=$true)][string]$WorkingDirectory,
  [Parameter(Mandatory=$true)][string]$StdoutLog,
  [Parameter(Mandatory=$true)][string]$StderrLog,
  [Parameter(Mandatory=$true)][string]$LicenseKey,
  [Parameter(Mandatory=$true)][string]$ModeChoice,
  [Parameter(Mandatory=$true)][string]$InstanceCount,
  [Parameter(Mandatory=$true)][string]$ModelPath,
  [Parameter(Mandatory=$true)][string]$DbPath,
  [Parameter(Mandatory=$true)][string]$ChildPidFile
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $ServerExe
$psi.WorkingDirectory = $WorkingDirectory
$psi.Arguments = "-host 0.0.0.0"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi

$stdoutWriter = [System.IO.StreamWriter]::new($StdoutLog, $false, [System.Text.Encoding]::UTF8)
$stderrWriter = [System.IO.StreamWriter]::new($StderrLog, $false, [System.Text.Encoding]::UTF8)

$proc.add_OutputDataReceived({
  param($sender, $e)
  if ($null -ne $e.Data) {
    $stdoutWriter.WriteLine($e.Data)
    $stdoutWriter.Flush()
  }
})

$proc.add_ErrorDataReceived({
  param($sender, $e)
  if ($null -ne $e.Data) {
    $stderrWriter.WriteLine($e.Data)
    $stderrWriter.Flush()
  }
})

$proc.Start() | Out-Null
$proc.BeginOutputReadLine()
$proc.BeginErrorReadLine()

"$($proc.Id)" | Out-File -FilePath $ChildPidFile -Encoding ascii -Force

Start-Sleep -Milliseconds 500

$proc.StandardInput.WriteLine($LicenseKey)
$proc.StandardInput.WriteLine($ModeChoice)
$proc.StandardInput.WriteLine($InstanceCount)
$proc.StandardInput.WriteLine($ModelPath)
$proc.StandardInput.WriteLine($DbPath)
$proc.StandardInput.Flush()

# 중요: StandardInput.Close() 호출하지 않음.
# qfe-server.exe가 콘솔 stdin EOF를 종료 신호처럼 처리할 가능성이 있어서 열어둠.

$proc.WaitForExit()

$stdoutWriter.Flush()
$stderrWriter.Flush()
$stdoutWriter.Close()
$stderrWriter.Close()

exit $proc.ExitCode