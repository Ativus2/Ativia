param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$ManifestFile = '',
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
if (-not $ManifestFile) { $ManifestFile = Join-Path $root 'benchmark\wechat-recovery\20260929-recovery\patch-manifest.json' }
$manifestPath = (Resolve-Path -LiteralPath $ManifestFile).Path
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([IO.Path]::GetFullPath($manifest.project).TrimEnd('\') -ne $root) { throw 'Manifest project does not match requested root' }
$backupRoot = Split-Path -Parent $manifestPath
function Resolve-ContainedFile([string]$base, [string]$relative) {
    if ([IO.Path]::IsPathRooted($relative)) { throw 'Absolute manifest path rejected' }
    $resolved = [IO.Path]::GetFullPath((Join-Path $base $relative))
    if (-not $resolved.StartsWith($base.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Manifest path escapes intended directory'
    }
    return $resolved
}
$checked = foreach ($entry in $manifest.files) {
    $target = Resolve-ContainedFile $root $entry.path
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) { throw "Missing target: $($entry.path)" }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $entry.after_sha256) {
        throw "Changed since patch; refusing any rollback: $($entry.path)"
    }
    $backup = $null
    if ($entry.existed_before) {
        $backup = Resolve-ContainedFile $backupRoot $entry.backup
        if ((Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash -ne $entry.before_sha256) {
            throw "Backup integrity mismatch: $($entry.path)"
        }
    }
    [pscustomobject]@{entry=$entry;target=$target;backup=$backup}
}
# Validate every file before performing any change. No recursive operations,
# process operations, client data/config writes, or guessed rollback paths.
if ($Apply) {
    foreach ($item in $checked) {
        if ($item.backup) {
            Copy-Item -LiteralPath $item.backup -Destination $item.target -Force
            if ((Get-FileHash -LiteralPath $item.target).Hash -ne $item.entry.before_sha256) { throw 'Restore verification failed' }
        } else {
            Remove-Item -LiteralPath $item.target
        }
    }
}
[ordered]@{
    status = $(if($Apply){'ROLLED_BACK'}else{'VERIFIED_ONLY'})
    modified_files_restored = @($checked | Where-Object backup).Count
    new_files_removed = @($checked | Where-Object { -not $_.backup }).Count
    reports_and_evidence_retained = $true
    production_client_config_and_processes_touched = $false
} | ConvertTo-Json

