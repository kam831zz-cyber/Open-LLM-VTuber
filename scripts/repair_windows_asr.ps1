$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$CheckScript = Join-Path $ScriptDir "check_windows_asr_compat.ps1"

$ExpectedFasterWhisper = "1.2.1"
$ExpectedAv = "17.0.0"
$ExpectedCTranslate2 = "4.7.2"
$ExpectedOnnxruntime = "1.20.1"

function Get-PackageVersion {
    param([string]$Package)

    $code = "import importlib.metadata as m, sys; print(m.version(sys.argv[1]))"
    try {
        $value = & $Python -c $code $Package 2>$null
        if ($LASTEXITCODE -eq 0 -and $value) {
            return [string]$value
        }
    } catch {
        return $null
    }
    return $null
}

function Write-VersionSummary {
    param(
        [string]$FasterWhisper,
        [string]$Av,
        [string]$CTranslate2,
        [string]$Onnxruntime
    )

    Write-Host "faster-whisper: $FasterWhisper"
    Write-Host "av: $Av"
    Write-Host "ctranslate2: $CTranslate2"
    Write-Host "onnxruntime: $Onnxruntime"
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "[ASR REPAIR] FAILED"
    Write-Host "Python not found: $Python"
    exit 1
}

$fasterWhisperVersion = Get-PackageVersion "faster-whisper"
$avVersion = Get-PackageVersion "av"
$ctranslate2Version = Get-PackageVersion "ctranslate2"
$onnxruntimeVersion = Get-PackageVersion "onnxruntime"

Write-Host "[ASR REPAIR] Current versions"
Write-VersionSummary $fasterWhisperVersion $avVersion $ctranslate2Version $onnxruntimeVersion
Write-Host "[ASR REPAIR] Target versions"
Write-Host "av -> $ExpectedAv"
Write-Host "ctranslate2 -> $ExpectedCTranslate2"
Write-Host "faster-whisper must already be $ExpectedFasterWhisper"
Write-Host "onnxruntime verified value is $ExpectedOnnxruntime, but this repair does not change it."

if ($fasterWhisperVersion -ne $ExpectedFasterWhisper) {
    Write-Host "[ASR REPAIR] FAILED"
    Write-Host "faster-whisper $fasterWhisperVersion detected. Expected $ExpectedFasterWhisper."
    Write-Host "This script only repairs av and ctranslate2 to avoid broad dependency changes."
    exit 1
}

$needsRepair = ($avVersion -ne $ExpectedAv) -or ($ctranslate2Version -ne $ExpectedCTranslate2)
if ($needsRepair) {
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCommand) {
        Write-Host "[ASR REPAIR] FAILED"
        Write-Host "uv command not found."
        exit 1
    }

    Write-Host "[ASR REPAIR] Running repair command"
    Write-Host "uv pip install --python $Python --reinstall --no-deps av==$ExpectedAv ctranslate2==$ExpectedCTranslate2"
    & $uvCommand.Source pip install --python $Python --reinstall --no-deps "av==$ExpectedAv" "ctranslate2==$ExpectedCTranslate2"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ASR REPAIR] FAILED"
        Write-Host "uv pip install failed with exit code $LASTEXITCODE"
        exit 1
    }
} else {
    Write-Host "[ASR REPAIR] SKIP"
    Write-Host "ASR binary dependencies are already compatible. No package changes were made."
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $CheckScript
$checkExitCode = $LASTEXITCODE
if ($checkExitCode -ne 0) {
    Write-Host "[ASR REPAIR] FAILED"
    Write-Host "ASR compatibility check failed with exit code $checkExitCode"
    exit 1
}

$importCode = "import av; import ctranslate2; import faster_whisper; from faster_whisper import WhisperModel; print('import av OK'); print('import ctranslate2 OK'); print('import faster_whisper OK'); print('WhisperModel import OK')"
& $Python -c $importCode
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ASR REPAIR] FAILED"
    Write-Host "Import verification failed with exit code $LASTEXITCODE"
    exit 1
}

Write-Host "[ASR REPAIR] SUCCESS"
exit 0
