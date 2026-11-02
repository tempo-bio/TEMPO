param([Parameter(Mandatory=$true)][string]$Package,[Parameter(Mandatory=$true)][string]$TempoDirectory,[Parameter(Mandatory=$true)][string]$Evidence)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
New-Item -ItemType Directory -Force $Evidence | Out-Null
$manifest=Get-Content (Join-Path $Package 'package.json') -Raw | ConvertFrom-Json
$manager=Join-Path $Package 'Manage-Package.ps1'
function Run-Manager([string]$Mode,[bool]$Success=$true) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $manager -Mode $Mode -TempoDirectory $TempoDirectory -NonInteractive
    if ($Success -and $LASTEXITCODE -ne 0) { throw 'Package operation failed.' }
    if (-not $Success -and $LASTEXITCODE -eq 0) { throw 'An injected failure was accepted.' }
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
$exe=Join-Path $TempoDirectory 'TEMPO.exe'
$before=Hash $exe
$sentinel=Join-Path $TempoDirectory 'research-data-must-remain.txt'
Set-Content -LiteralPath $sentinel 'synthetic research-data sentinel'
$sentinelHash=Hash $sentinel
$header=Join-Path $Package 'payload\_internal\firmware\tempo_pacer\investigation_1.h'
$headerBytes=[IO.File]::ReadAllBytes($header)
try {
    Add-Content -LiteralPath $header '// injected corruption'
    Run-Manager Install $false
    if ((Hash $exe) -ne $before) { throw 'Failed validation changed the application.' }
} finally { [IO.File]::WriteAllBytes($header,$headerBytes) }
$lock=[IO.File]::Open($exe,'Open','ReadWrite','None')
try { Run-Manager Install $false } finally { $lock.Dispose() }
Run-Manager Install
Run-Manager Install $false
foreach ($file in $manifest.files) {
    if ((Hash (Join-Path $TempoDirectory $file.path)) -ne $file.after_sha256) { throw 'Installed payload hash differs.' }
}
$report=Join-Path ([IO.Path]::GetFullPath($Evidence)) 'installed-investigation.json'
$process=Start-Process $exe -ArgumentList @('--verify-stimulation-investigation',('"'+$report+'"')) -PassThru
if (-not $process.WaitForExit(240000)) { $process.Kill(); throw 'Installed package verification timed out.' }
$process.Refresh()
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) { throw 'Installed package verification failed.' }
$verified=Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
if (-not $verified.success -or -not $verified.simulated_instruments -or $verified.hardware_tested) { throw 'Installed verification report is invalid.' }
$installedHeader=Join-Path $TempoDirectory '_internal\firmware\tempo_pacer\investigation_1.h'
$old=[IO.File]::ReadAllBytes($installedHeader)
try {
    Add-Content -LiteralPath $installedHeader '// changed after installation'
    Run-Manager Remove $false
    if ((Hash $exe) -ne ($manifest.files | Where-Object path -eq 'TEMPO.exe').after_sha256) { throw 'Rejected removal changed application.' }
} finally { [IO.File]::WriteAllBytes($installedHeader,$old) }
Run-Manager Remove
if ((Hash $exe) -ne $before) { throw 'Removal did not restore original TEMPO.exe.' }
if ((Hash $sentinel) -ne $sentinelHash) { throw 'Package changed research data.' }
foreach ($file in $manifest.files) {
    $path=Join-Path $TempoDirectory $file.path
    if ($file.before_sha256) {
        if ((Hash $path) -ne $file.before_sha256) { throw 'An original resource was not restored.' }
    } elseif (Test-Path -LiteralPath $path) { throw 'A package-only resource remains after removal.' }
}
# The same exact package must be installable again after removal.
Run-Manager Install
Run-Manager Remove
[ordered]@{success=$true;original_executable_sha256=$before;preserved_research_data=$true;checks=@('corrupt payload rejected','running-file lock rejected','install verified','duplicate install rejected','changed-file removal rejected','original files restored','reinstall and remove')} |
    ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Evidence 'package-installation.json') -Encoding UTF8
