param([string]$Directory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.SelectVoice('Microsoft Zira Desktop')
$speaker.Rate = 0
$speaker.Volume = 100
$scenes = Get-Content -LiteralPath (Join-Path $Directory 'storyboard.json') -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    for ($index = 0; $index -lt $scenes.Count; $index++) {
        $outputPath = Join-Path $Directory ('narration-{0:D2}.wav' -f $index)
        $speaker.SetOutputToWaveFile($outputPath)
        $speaker.Speak($scenes[$index].narration)
        $speaker.SetOutputToNull()
    }
} finally {
    $speaker.Dispose()
}
Write-Output 'Generated ten local narration segments with Microsoft Zira Desktop.'
