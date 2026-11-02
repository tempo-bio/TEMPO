param(
    [Parameter(Mandatory=$true)][ValidateSet('Install','Remove')][string]$Mode,
    [string]$TempoDirectory = (Join-Path $env:LOCALAPPDATA 'Programs\TEMPO'),
    [switch]$NonInteractive
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

function Hash([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function Child([string]$Root, [string]$Relative) {
    if ($Relative -notmatch '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$' -or @($Relative.Split('/') | Where-Object { $_ -eq '..' -or $_ -eq '.' }).Count) {
        throw 'Invalid package path.'
    }
    $path=[IO.Path]::GetFullPath((Join-Path $Root $Relative.Replace('/',[IO.Path]::DirectorySeparatorChar)))
    if (-not $path.StartsWith($Root.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Package path leaves its folder.' }
    $parent=Split-Path -Parent $path
    while ($parent -and $parent.Length -ge $Root.Length) {
        if ((Test-Path -LiteralPath $parent) -and ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Package folders must not be links.' }
        $parent=Split-Path -Parent $parent
    }
    return $path
}
function Copy-Atomic([string]$Source,[string]$Destination) {
    $parent=Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary=Join-Path $parent ('.si1-'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        [IO.File]::Copy($Source,$temporary,$false)
        if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($temporary,$Destination,$null) }
        else { [IO.File]::Move($temporary,$Destination) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}
function Write-State($Value,[string]$Destination) {
    $temporary=$Destination+'.tmp'
    try {
        $json=ConvertTo-Json -InputObject $Value -Depth 12
        [IO.File]::WriteAllText($temporary,$json,(New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Destination)) { [IO.File]::Replace($temporary,$Destination,$null) }
        else { [IO.File]::Move($temporary,$Destination) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

if (-not (Test-Path -LiteralPath (Join-Path $TempoDirectory 'TEMPO.exe') -PathType Leaf)) {
    if ($NonInteractive) { throw 'An existing TEMPO v1.0.1 installation is required.' }
    $TempoDirectory=Read-Host 'TEMPO installation folder'
}
$TempoDirectory=[IO.Path]::GetFullPath($TempoDirectory).TrimEnd('\')
$executable=Join-Path $TempoDirectory 'TEMPO.exe'
if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'TEMPO.exe was not found in that folder.' }
try { $lock=[IO.File]::Open($executable,'Open','ReadWrite','None'); $lock.Dispose() }
catch { throw 'Close TEMPO before installing or removing this package.' }
$stateRoot=Join-Path $TempoDirectory '.tempo-packages\stimulation-investigation-1'
$receipt=Join-Path $stateRoot 'receipt.json'
$backup=Join-Path $stateRoot 'backup'

if ($Mode -eq 'Remove') {
    if (-not (Test-Path -LiteralPath $receipt)) { throw 'Stimulation Investigation 1 is not installed in this folder.' }
    $state=Get-Content -LiteralPath $receipt -Raw | ConvertFrom-Json
    if ($state.package -ne 'stimulation-investigation-1' -or $state.schema -ne 1) { throw 'The package receipt is invalid.' }
    foreach ($file in $state.files) {
        $target=Child $TempoDirectory $file.path
        $current=Hash $target
        $recovering=$state.state -in @('installing','rolled-back','removing')
        if ($current -ne $file.after_sha256 -and -not ($recovering -and $current -eq $file.before_sha256)) { throw ('Application files changed after installation. Nothing was restored: '+$file.path) }
        if ($file.before_sha256 -and (Hash (Child $backup $file.path)) -ne $file.before_sha256) { throw ('An original backup is missing or changed: '+$file.path) }
    }
    $state.state='removing'; Write-State $state $receipt
    foreach ($file in $state.files) {
        $target=Child $TempoDirectory $file.path
        if ($file.before_sha256) { Copy-Atomic (Child $backup $file.path) $target }
        else { [IO.File]::Delete($target) }
    }
    Remove-Item -LiteralPath $stateRoot -Recurse -Force
    Write-Output 'Stimulation Investigation 1 removed. Original TEMPO files restored.'
    exit 0
}

$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package.json') -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $manifest.package -ne 'stimulation-investigation-1' -or $manifest.tempo_version -ne '1.0.1') { throw 'Unsupported package manifest.' }
if (Test-Path -LiteralPath $receipt) { throw 'This package already has an installation or recovery receipt. Run Remove.cmd before reinstalling.' }
$identityPath=Join-Path $TempoDirectory '_internal\assets\build-provenance.json'
$identity=Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
if ($identity.commit -ne $manifest.base_commit) { throw 'This package requires the stable TEMPO v1.0.1 installation.' }
$seen=New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($file in $manifest.files) {
    if (-not $seen.Add($file.path)) { throw 'Duplicate package destination.' }
    $target=Child $TempoDirectory $file.path
    $source=Child (Join-Path $PSScriptRoot 'payload') $file.path
    if ((Hash $source) -ne $file.after_sha256 -or (Get-Item -LiteralPath $source).Length -ne $file.size) { throw ('Package file is missing or changed: '+$file.path) }
    if ((Hash $target) -ne $file.before_sha256) { throw ('The installed application differs from the supported v1.0.1 build: '+$file.path) }
}
foreach ($file in $manifest.runtime) {
    if ((Hash (Child $TempoDirectory $file.path)) -ne $file.sha256) { throw ('The installed runtime differs from the supported build: '+$file.path) }
}
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($file in $manifest.files) {
    if ($file.before_sha256) { Copy-Atomic (Child $TempoDirectory $file.path) (Child $backup $file.path) }
}
$state=[ordered]@{schema=1;package=$manifest.package;version=$manifest.version;source_sha256=$manifest.source_sha256;state='installing';files=$manifest.files}
Write-State $state $receipt
$touched=New-Object Collections.Generic.List[object]
try {
    foreach ($file in $manifest.files) {
        $touched.Add($file)
        Copy-Atomic (Child (Join-Path $PSScriptRoot 'payload') $file.path) (Child $TempoDirectory $file.path)
    }
    $state.state='installed'; Write-State $state $receipt
} catch {
    $failure=$_
    foreach ($file in $touched) {
        $target=Child $TempoDirectory $file.path
        if ($file.before_sha256) { Copy-Atomic (Child $backup $file.path) $target }
        elseif (Test-Path -LiteralPath $target) { [IO.File]::Delete($target) }
    }
    $state.state='rolled-back'; Write-State $state $receipt
    throw $failure
}
Write-Output 'Stimulation Investigation 1 installed. Reopen TEMPO and select it in Acquisition.'
