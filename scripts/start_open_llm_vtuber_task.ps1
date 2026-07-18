$ErrorActionPreference = "Stop"

$Root = "C:\home-ai\Open-LLM-VTuber"
$Port = 12393
$HostAddress = "127.0.0.1"
$DiagnosticsDir = Join-Path $Root "diagnostics"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RunLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.log"
$StdoutLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.stdout.log"
$StderrLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.stderr.log"

function Write-RunLog {
  param([string]$Message)
  $line = "{0} {1}" -f (Get-Date).ToString("o"), $Message
  Add-Content -LiteralPath $RunLog -Value $line -Encoding utf8
}

function Test-PortOpen {
  param(
    [string]$Address,
    [int]$PortNumber
  )

  $client = [Net.Sockets.TcpClient]::new()
  try {
    $connect = $client.BeginConnect($Address, $PortNumber, $null, $null)
    if (-not $connect.AsyncWaitHandle.WaitOne(1000)) {
      return $false
    }
    $client.EndConnect($connect)
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
  }
}

New-Item -ItemType Directory -Path $DiagnosticsDir -Force | Out-Null

Write-RunLog "start_time=$((Get-Date).ToString("o"))"
Write-RunLog "working_directory=$Root"
Write-RunLog "port_check=$HostAddress`:$Port"
Write-RunLog "command=uv run run_server.py"
Write-RunLog "stdout=$StdoutLog"
Write-RunLog "stderr=$StderrLog"

if (Test-PortOpen -Address $HostAddress -PortNumber $Port) {
  Write-RunLog "port_status=already_listening"
  Write-RunLog "exit_time=$((Get-Date).ToString("o"))"
  Write-RunLog "exit_code=0"
  exit 0
}

$AsrCheckScript = Join-Path $Root "scripts\check_windows_asr_compat.ps1"
if (Test-Path -LiteralPath $AsrCheckScript) {
  Write-RunLog "asr_check=start"
  try {
    $asrCheckOutput = & powershell -NoProfile -ExecutionPolicy Bypass -File $AsrCheckScript 2>&1
    $asrCheckExitCode = $LASTEXITCODE
    foreach ($line in $asrCheckOutput) {
      Write-Host $line
      Write-RunLog "asr_check_output=$line"
    }
    Write-RunLog "asr_check_exit_code=$asrCheckExitCode"
    if ($asrCheckExitCode -ne 0) {
      Write-Warning "ASR compatibility check returned exit code $asrCheckExitCode. Startup will continue."
      Write-RunLog "asr_check_warning=continuing_startup"
    }
  } catch {
    Write-Warning "ASR compatibility check failed: $($_.Exception.Message). Startup will continue."
    Write-RunLog "asr_check_error=$($_.Exception.Message)"
  }
} else {
  Write-Warning "ASR compatibility check script not found: $AsrCheckScript. Startup will continue."
  Write-RunLog "asr_check=missing"
}

if (Test-Path -LiteralPath (Join-Path $Root ".venv\Lib\site-packages\onnxruntime\capi")) {
  $env:PATH = (Join-Path $Root ".venv\Lib\site-packages\onnxruntime\capi") + ";" + $env:PATH
}

$uv = Get-Command uv -ErrorAction Stop
$process = Start-Process `
  -FilePath $uv.Source `
  -ArgumentList @("run", "run_server.py") `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $StdoutLog `
  -RedirectStandardError $StderrLog `
  -WindowStyle Hidden `
  -PassThru

Write-RunLog "pid=$($process.Id)"
Start-Sleep -Seconds 5
Write-RunLog "port_status_after_start=$(if (Test-PortOpen -Address $HostAddress -PortNumber $Port) { "listening" } else { "not_listening" })"

$process.WaitForExit()

Write-RunLog "exit_time=$((Get-Date).ToString("o"))"
Write-RunLog "exit_code=$($process.ExitCode)"
exit $process.ExitCode
