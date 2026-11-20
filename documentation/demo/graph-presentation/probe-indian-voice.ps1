$ErrorActionPreference = 'Stop'
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media.SpeechSynthesis, ContentType=WindowsRuntime]
[Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Select-Object DisplayName, Language, Id | ConvertTo-Json
