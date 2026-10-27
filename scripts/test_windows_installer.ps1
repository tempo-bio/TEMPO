$ErrorActionPreference='Stop'
$Evidence=(Get-Location).Path
. (Join-Path $PSScriptRoot 'windows_installer_helpers.ps1')
function Expect-Failure([scriptblock]$Action, [string]$Expected = '') {
    $failed=$false
    try { & $Action } catch {
        if ($Expected -and $_.Exception.Message -notmatch $Expected) { throw }
        $failed=$true
    }
    if (-not $failed) { throw 'Injected failure was incorrectly accepted' }
}
$contract=[pscustomobject]@{schema_version=2;commit='fixture';lock_sha256='hash';required_checks=@('a','b')}
$report=[pscustomobject]@{schema_version=2;commit='fixture';frozen=$true;success=$true;failures=@();checks=@('a','b');required_checks=@('a','b');runtime_details=@{'bundled payload'=@{identity=@{lock_sha256='hash'}}}}
Assert-Verification $report $contract
$report.success=$false;Expect-Failure { Assert-Verification $report $contract };$report.success=$true
$report.checks=@('a');Expect-Failure { Assert-Verification $report $contract };$report.checks=@('a','b')
$report.commit='foreign';Expect-Failure { Assert-Verification $report $contract };$report.commit='fixture'
Invoke-Bounded "$env:SystemRoot\System32\cmd.exe" @('/c','exit','0') 10
Expect-Failure { Invoke-Bounded "$env:SystemRoot\System32\cmd.exe" @('/c','exit','7') 10 }
Expect-Failure { Invoke-Bounded "$env:SystemRoot\System32\ping.exe" @('127.0.0.1','-n','10') 1 }
$target=Join-Path $env:RUNNER_TEMP 'TEMPO removal failure fixture'
New-Item -ItemType Directory -Force $target | Out-Null
try {
    New-Item -ItemType File -Force (Join-Path $target 'TEMPO.exe') | Out-Null
    Expect-Failure { Assert-Uninstalled $target }
    Remove-Item (Join-Path $target 'TEMPO.exe')
    New-Item -ItemType Directory (Join-Path $target '_internal') | Out-Null
    Expect-Failure { Assert-Uninstalled $target }
    Remove-Item (Join-Path $target '_internal')
    Assert-Uninstalled $target
    $stub = Join-Path $target 'unins000.exe'
    New-Item -ItemType File $stub | Out-Null
    Expect-Failure { Assert-Uninstalled $target 0 } 'cleanup did not complete'
    $cleanup = Start-Job -ScriptBlock { param($Path) Remove-Item -LiteralPath $Path } -ArgumentList $stub
    try {
        Assert-Uninstalled $target 10
        if (-not (Wait-Job $cleanup -Timeout 10)) { throw 'Simulated uninstaller cleanup did not settle' }
        Receive-Job $cleanup -ErrorAction Stop | Out-Null
    } finally { Stop-Job $cleanup; Remove-Job $cleanup }
    New-Item -ItemType File (Join-Path $target 'unexpected.dll') | Out-Null
    Expect-Failure { Assert-Uninstalled $target 0 } 'unexpected files'
    Remove-Item (Join-Path $target 'unexpected.dll')
    # Deterministically remove the root after Test-Path but before enumeration.
    function Get-ChildItem {
        param([string]$LiteralPath, [switch]$Recurse, [switch]$Force, [switch]$File, [string]$ErrorAction)
        Remove-Item -LiteralPath $LiteralPath -Recurse -Force
        Microsoft.PowerShell.Management\Get-ChildItem -LiteralPath $LiteralPath -Recurse -Force -File -ErrorAction Stop
    }
    try {
        Assert-Uninstalled $target
        if (@(Get-RemainingInstallationFiles $target).Count) { throw 'Removed root reported remaining files' }
    } finally { Remove-Item Function:\Get-ChildItem }
    New-Item -ItemType Directory -Force $target | Out-Null
    function Get-ChildItem {
        param([string]$LiteralPath, [switch]$Recurse, [switch]$Force, [switch]$File, [string]$ErrorAction)
        throw [System.Management.Automation.ItemNotFoundException]::new('Injected missing child with existing root')
    }
    try {
        Expect-Failure { Assert-Uninstalled $target } 'missing child with existing root'
    } finally { Remove-Item Function:\Get-ChildItem }
    function Get-ChildItem {
        param([string]$LiteralPath, [switch]$Recurse, [switch]$Force, [switch]$File, [string]$ErrorAction)
        throw [System.UnauthorizedAccessException]::new('Injected enumeration access failure')
    }
    try {
        Expect-Failure { Assert-Uninstalled $target } 'enumeration access failure'
    } finally { Remove-Item Function:\Get-ChildItem }
} finally { Remove-Item $target -Recurse -Force }
'All installer failure injections passed.'
