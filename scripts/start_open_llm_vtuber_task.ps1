$ErrorActionPreference = "Stop"

$Root = "C:\home-ai\Open-LLM-VTuber"
$Port = 12393
$HostAddress = "127.0.0.1"
$DiagnosticsDir = Join-Path $Root "diagnostics"
$RuntimeDir = Join-Path $DiagnosticsDir "runtime"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$RunLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.log"
$StdoutLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.stdout.log"
$StderrLog = Join-Path $DiagnosticsDir "open_llm_vtuber_task_$Timestamp.stderr.log"
$RuntimeLog = Join-Path $RuntimeDir "open_llm_runtime_$((Get-Date).ToString('yyyyMMdd')).log"

function Write-RunLog {
  param([string]$Message)
  $line = "{0} {1}" -f (Get-Date).ToString("o"), $Message
  Add-Content -LiteralPath $RunLog -Value $line -Encoding utf8
  Add-Content -LiteralPath $RuntimeLog -Value $line -Encoding utf8
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

function Get-PortOwnerProcessId {
  param([int]$PortNumber)
  try {
    $conn = Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
      return $conn.OwningProcess
    }
  } catch {
    Write-RunLog "port_owner_lookup_error=$($_.Exception.Message)"
  }
  return $null
}

function Write-ProcessTreeSnapshot {
  param(
    [string]$Label,
    [int]$RootProcessId
  )

  try {
    $allProcesses = @(Get-CimInstance Win32_Process)
    $ids = New-Object System.Collections.Generic.List[int]
    $ids.Add($RootProcessId)
    $index = 0

    while ($index -lt $ids.Count) {
      $parentId = $ids[$index]
      $index += 1
      foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $parentId }) {
        $childId = [int]$child.ProcessId
        if (-not $ids.Contains($childId)) {
          $ids.Add($childId)
        }
      }
    }

    foreach ($proc in $allProcesses | Where-Object { $ids.Contains([int]$_.ProcessId) } | Sort-Object ProcessId) {
      $commandLine = ($proc.CommandLine -replace "`r?`n", " ")
      Write-RunLog "$Label process pid=$($proc.ProcessId) ppid=$($proc.ParentProcessId) exe=$($proc.ExecutablePath) command=$commandLine"
    }
  } catch {
    Write-RunLog "$Label process_tree_error=$($_.Exception.Message)"
  }
}

New-Item -ItemType Directory -Path $DiagnosticsDir -Force | Out-Null
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

$exitCode = 1
$process = $null

Write-RunLog "[START] timestamp=$((Get-Date).ToString("o"))"
Write-RunLog "wrapper_pid=$PID"
Write-RunLog "working_directory=$Root"
Write-RunLog "port_check=$HostAddress`:$Port"
Write-RunLog "command=uv run --no-sync run_server.py"
Write-RunLog "stdout=$StdoutLog"
Write-RunLog "stderr=$StderrLog"

try {
  if (Test-PortOpen -Address $HostAddress -PortNumber $Port) {
    Write-RunLog "port_status=already_listening"
    $ownerPid = Get-PortOwnerProcessId -PortNumber $Port
    if ($ownerPid) {
      Write-RunLog "existing_port_owner_pid=$ownerPid"
    }
    $exitCode = 0
    return
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
  Write-RunLog "uv_path=$($uv.Source)"
  $process = Start-Process `
    -FilePath $uv.Source `
    -ArgumentList @("run", "--no-sync", "run_server.py") `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -WindowStyle Hidden `
    -PassThru

  Write-RunLog "uv_pid=$($process.Id)"
  Write-ProcessTreeSnapshot -Label "after_start" -RootProcessId $process.Id

  $portListening = $false
  for ($i = 1; $i -le 24; $i += 1) {
    Start-Sleep -Seconds 5
    if (Test-PortOpen -Address $HostAddress -PortNumber $Port) {
      $portListening = $true
      break
    }
    if ($process.HasExited) {
      break
    }
  }

  Write-RunLog "port_status_after_start=$(if ($portListening) { "listening" } else { "not_listening" })"
  $serverPid = Get-PortOwnerProcessId -PortNumber $Port
  if ($serverPid) {
    Write-RunLog "server_pid=$serverPid"
  }
  Write-ProcessTreeSnapshot -Label "after_port_check" -RootProcessId $process.Id

  $process.WaitForExit()
  $exitCode = $process.ExitCode
  Write-RunLog "uv_process_has_exited=$($process.HasExited)"
  Write-ProcessTreeSnapshot -Label "after_exit" -RootProcessId $process.Id
} catch {
  Write-RunLog "[ERROR] timestamp=$((Get-Date).ToString("o"))"
  Write-RunLog "wrapper_exception_type=$($_.Exception.GetType().FullName)"
  Write-RunLog "wrapper_exception_message=$($_.Exception.Message)"
  $exitCode = 1
} finally {
  Write-RunLog "[EXIT] timestamp=$((Get-Date).ToString("o"))"
  Write-RunLog "exit_code=$exitCode"
  Write-RunLog "port_status_on_exit=$(if (Test-PortOpen -Address $HostAddress -PortNumber $Port) { "listening" } else { "not_listening" })"
  Write-RunLog "stdout=$StdoutLog"
  Write-RunLog "stderr=$StderrLog"
}

exit $exitCode
