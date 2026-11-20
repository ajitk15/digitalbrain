# Records each storyboard sentence separately with the local en-IN voice, at a set engine speaking rate.
# Run with Windows PowerShell 5.1 (WinRT speech): powershell.exe -ExecutionPolicy Bypass -File narrate.ps1
param([string]$Voice = 'Microsoft Heera', [double]$Rate = 1.2)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesisStream, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | Select-Object -First 1
function Await-Stream($operation) {
    $task = $asTask.MakeGenericMethod([Windows.Media.SpeechSynthesis.SpeechSynthesisStream]).Invoke($null, @($operation))
    $task.Wait()
    return $task.Result
}
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$selected = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -eq 'en-IN' -and $_.DisplayName -eq $Voice } | Select-Object -First 1
if (!$selected) { throw "Indian English voice '$Voice' unavailable" }
$synth.Voice = $selected
# The engine's own speaking rate, not a time-stretch of finished audio.
$synth.Options.SpeakingRate = $Rate
$out = Join-Path $PSScriptRoot 'speech'
New-Item -ItemType Directory -Force $out | Out-Null
$lines = Get-Content (Join-Path $PSScriptRoot 'spoken.json') -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    foreach ($line in $lines) {
        $stream = Await-Stream ($synth.SynthesizeTextToStreamAsync($line.say))
        $reader = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)
        $output = [System.IO.File]::Create((Join-Path $out $line.file))
        try { $reader.CopyTo($output) } finally { $output.Dispose(); $reader.Dispose(); $stream.Dispose() }
    }
    Write-Output ("Recorded {0} sentences: {1} / en-IN" -f $lines.Count, $Voice)
} finally { $synth.Dispose() }
