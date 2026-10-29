# Read-only check of the fixed TEMPO settings cache. Never reads credentials.
$ErrorActionPreference = 'Stop'
$result = [ordered]@{schema_version=2; captured_utc=[DateTime]::UtcNow.ToString('o'); processes=@(); cache_exists=$false; settings=@(); preferences_readable=$false; incomplete_files=0}
$keys = @('colab_notebook_url','hf_org_id','hf_dataset_repo','hf_models_repo')
$prefs = $null
try {
    $prefPath = Join-Path $env:LOCALAPPDATA 'TEMPO\config\tempo_preferences.json'
    if (Test-Path -LiteralPath $prefPath) {
        $prefs = Get-Content -LiteralPath $prefPath -Raw | ConvertFrom-Json
        $result.preferences_readable = $true
    }
} catch { $result.preferences_error_type = $_.Exception.GetType().Name }
foreach ($app in @(Get-Process -Name TEMPO -ErrorAction SilentlyContinue)) {
    try {
        $result.processes += [ordered]@{pid=$app.Id; started_utc=$app.StartTime.ToUniversalTime().ToString('o'); memory_mb=[math]::Round($app.WorkingSet64/1MB,1)}
    } catch { $result.processes += [ordered]@{pid=$app.Id; metadata_unavailable=$true} }
}
$cache = $env:HF_HUB_CACHE
if (-not $cache) { $cache = $env:HUGGINGFACE_HUB_CACHE }
if (-not $cache) {
    $hfHome = $env:HF_HOME
    if (-not $hfHome) {
        $base = $env:XDG_CACHE_HOME
        if (-not $base) { $base = Join-Path $env:USERPROFILE '.cache' }
        $hfHome = Join-Path $base 'huggingface'
    }
    $cache = Join-Path $hfHome 'hub'
}
$repo = Join-Path $cache 'models--tempo-bio-settings--workspace'
$result.cache_exists = Test-Path -LiteralPath $repo
$result.helper_environment_progress_bars_disabled = $env:HF_HUB_DISABLE_PROGRESS_BARS -eq '1'
if ($result.cache_exists) {
    $result.repository_readable = $false
    try {
        $null = @(Get-ChildItem -LiteralPath $repo -Force -ErrorAction Stop)
        $result.repository_readable = $true
    } catch { $result.repository_error_type = $_.Exception.GetType().Name }
    $snapshots = Join-Path $repo 'snapshots'
    $allDirectories = @(Get-ChildItem -LiteralPath $snapshots -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending)
    $result.snapshot_directories = $allDirectories.Count
    $result.scan_truncated = $allDirectories.Count -gt 20
    $directories = @($allDirectories | Select-Object -First 20)
    $result.cached_missing_file_markers = @()
    $missingRoot = Join-Path $repo '.no_exist'
    foreach ($directory in @(Get-ChildItem -LiteralPath $missingRoot -Directory -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 20)) {
        $marker = Join-Path $directory.FullName 'tempo_shared_settings.json'
        if (Test-Path -LiteralPath $marker) {
            $result.cached_missing_file_markers += (Get-Item -LiteralPath $marker).LastWriteTimeUtc.ToString('o')
        }
    }
    foreach ($directory in $directories) {
        $file = Join-Path $directory.FullName 'tempo_shared_settings.json'
        if (-not (Test-Path -LiteralPath $file)) { continue }
        $entry = [ordered]@{bytes=0; schema_valid=$false; fields_valid=$false; matches_saved_settings=$null}
        try {
            $item = Get-Item -LiteralPath $file
            $entry.bytes = $item.Length
            $entry.modified_utc = $item.LastWriteTimeUtc.ToString('o')
            $entry.sha256 = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLower()
            if ($item.Length -gt 65536) { throw 'Oversized settings' }
            $doc = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $entry.schema_valid = $doc.schema_version -eq 2 -and $doc.updated_by -cmatch '^hf:[A-Za-z0-9_-]+$'
            $actual = @($doc.settings.PSObject.Properties.Name | Sort-Object)
            $entry.fields_valid = ($actual -join ',') -ceq (($keys | Sort-Object) -join ',')
            foreach ($key in $keys) {
                $value = $doc.settings.$key
                if ($value -isnot [string] -or $value.Length -gt 2048 -or $value -cmatch 'hf_|hf:') { $entry.fields_valid = $false }
            }
            if ($entry.fields_valid) {
                $org = $doc.settings.hf_org_id.Trim()
                if ($org -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*$') { $entry.fields_valid = $false }
                foreach ($key in @('hf_dataset_repo','hf_models_repo')) {
                    if ($doc.settings.$key.Trim() -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$') { $entry.fields_valid = $false }
                }
                if ($doc.settings.hf_dataset_repo.Trim().Split('/')[0] -cne $org) { $entry.fields_valid = $false }
                $link = $doc.settings.colab_notebook_url.Trim()
                if ($link) {
                    $uri = $null
                    if (-not [Uri]::TryCreate($link,[UriKind]::Absolute,[ref]$uri)) { $entry.fields_valid = $false }
                    elseif ($uri.Scheme -ne 'https' -or $uri.Host -ne 'colab.research.google.com' -or $uri.UserInfo -or $uri.AbsolutePath -cnotmatch '^/(drive|github)/') { $entry.fields_valid = $false }
                }
            }
            if ($entry.fields_valid -and $prefs) {
                $same = $true
                foreach ($key in $keys) { if ($doc.settings.$key.Trim() -cne $prefs.collaboration.$key.value) { $same = $false } }
                $entry.matches_saved_settings = $same
            }
        } catch { $entry.read_error_type = $_.Exception.GetType().Name }
        $result.settings += $entry
    }
    $blobs = Join-Path $repo 'blobs'
    $result.incomplete_files = @(Get-ChildItem -LiteralPath $blobs -File -Filter '*.incomplete' -ErrorAction SilentlyContinue).Count
}
$outputRoot = $env:TEMPO_CACHE_REPORT_ROOT
if (-not $outputRoot) { $outputRoot = $PSScriptRoot }
$report = Join-Path $outputRoot 'TEMPO-settings-cache-report.json'
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $report -Encoding UTF8
Write-Host 'Finished. Share TEMPO-settings-cache-report.json from this folder.'
