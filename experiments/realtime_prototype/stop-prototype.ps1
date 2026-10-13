$scriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'server.py')).Path
$matchingProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -and $_.CommandLine.Contains($scriptPath)
}
foreach ($matchingProcess in $matchingProcesses) {
    Stop-Process -Id $matchingProcess.ProcessId -ErrorAction SilentlyContinue
}
Write-Output 'Ativia prototype stopped. The original assistant and voice service were not targeted.'
