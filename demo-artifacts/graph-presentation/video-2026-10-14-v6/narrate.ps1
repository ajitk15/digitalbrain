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
$voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.Language -eq 'en-IN' -and $_.DisplayName -eq 'Microsoft Heera' } | Select-Object -First 1
if (!$voice) { throw 'Indian English voice unavailable' }
$synth.Voice = $voice
$scenes = Get-Content (Join-Path $PSScriptRoot 'storyboard.json') -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    for ($i = 0; $i -lt $scenes.Count; $i++) {
        $stream = Await-Stream ($synth.SynthesizeTextToStreamAsync($scenes[$i].narration))
        $reader = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)
        $output = [System.IO.File]::Create((Join-Path $PSScriptRoot ('narration-{0:D2}.wav' -f $i)))
        try { $reader.CopyTo($output) } finally { $output.Dispose(); $reader.Dispose(); $stream.Dispose() }
        Write-Output ('Recorded scene {0}: Microsoft Heera / en-IN' -f ($i+1))
    }
} finally { $synth.Dispose() }
