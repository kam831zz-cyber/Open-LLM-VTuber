param(
    [string]$MockFasterWhisperVersion = "",
    [string]$MockAvVersion = "",
    [string]$MockCTranslate2Version = "",
    [string]$MockOnnxruntimeVersion = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"

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

function Format-VersionLine {
    param([string]$Name, [string]$Version)
    if ([string]::IsNullOrWhiteSpace($Version)) {
        return "${Name}: not installed"
    }
    return "${Name}: $Version"
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "[ASR CHECK] FAILED"
    Write-Host "Python not found: $Python"
    exit 1
}

$fasterWhisperVersion = if ($MockFasterWhisperVersion) { $MockFasterWhisperVersion } else { Get-PackageVersion "faster-whisper" }
$avVersion = if ($MockAvVersion) { $MockAvVersion } else { Get-PackageVersion "av" }
$ctranslate2Version = if ($MockCTranslate2Version) { $MockCTranslate2Version } else { Get-PackageVersion "ctranslate2" }
$onnxruntimeVersion = if ($MockOnnxruntimeVersion) { $MockOnnxruntimeVersion } else { Get-PackageVersion "onnxruntime" }

$warnings = New-Object System.Collections.Generic.List[string]

if ($fasterWhisperVersion -ne $ExpectedFasterWhisper) {
    $warnings.Add("faster-whisper $fasterWhisperVersion detected. Expected: faster-whisper $ExpectedFasterWhisper")
}
if ($avVersion -ne $ExpectedAv) {
    $warnings.Add("av $avVersion detected. Expected: av $ExpectedAv")
}
if ($ctranslate2Version -ne $ExpectedCTranslate2) {
    $warnings.Add("ctranslate2 $ctranslate2Version detected. Expected: ctranslate2 $ExpectedCTranslate2")
}

$onnxruntimeNote = $null
if ($onnxruntimeVersion -ne $ExpectedOnnxruntime) {
    $onnxruntimeNote = "onnxruntime $onnxruntimeVersion detected. Current verified value: onnxruntime $ExpectedOnnxruntime. This alone does not block startup."
}

if ($warnings.Count -eq 0) {
    Write-Host "[ASR CHECK] OK"
    Write-Host (Format-VersionLine "faster-whisper" $fasterWhisperVersion)
    Write-Host (Format-VersionLine "av" $avVersion)
    Write-Host (Format-VersionLine "ctranslate2" $ctranslate2Version)
    Write-Host (Format-VersionLine "onnxruntime" $onnxruntimeVersion)
    if ($onnxruntimeNote) {
        Write-Host "[ASR CHECK] NOTE"
        Write-Host $onnxruntimeNote
    }
    exit 0
}

Write-Host "[ASR CHECK] WARNING"
foreach ($warning in $warnings) {
    Write-Host $warning
}
Write-Host (Format-VersionLine "onnxruntime" $onnxruntimeVersion)
if ($onnxruntimeNote) {
    Write-Host $onnxruntimeNote
}
Write-Host "Komugi faster-whisper ASR may be blocked by Windows Code Integrity / WDAC."
Write-Host "Run manually:"
Write-Host "powershell -ExecutionPolicy Bypass -File scripts\repair_windows_asr.ps1"
exit 2

