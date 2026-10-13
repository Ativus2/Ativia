$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$fixtureRoot = Join-Path $projectRoot 'docs/realtime-benchmark/fixtures'
$prompts = Get-Content -LiteralPath (Join-Path $fixtureRoot 'prompts.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$speaker = New-Object -ComObject SAPI.SpVoice
$voice = @($speaker.GetVoices() | Where-Object { $_.GetDescription() -like '*Huihui*' })[0]
if ($null -eq $voice) { throw 'Chinese Windows SAPI voice missing' }
$speaker.Voice = $voice
$speaker.Rate = 0
for ($index = 0; $index -lt $prompts.Count; $index++) {
    $path = Join-Path $fixtureRoot ('{0:D2}.wav' -f ($index + 1))
    $stream = New-Object -ComObject SAPI.SpFileStream
    try {
        $stream.Open($path, 3, $false)
        $speaker.AudioOutputStream = $stream
        [void]$speaker.Speak($prompts[$index])
    } finally { $stream.Close() }
}
Write-Output "Created $($prompts.Count) synthetic Chinese input recordings with Windows SAPI."
