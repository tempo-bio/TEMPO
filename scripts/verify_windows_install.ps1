param(
    [Parameter(Mandatory)][ValidateSet('install','verify','uninstall')][string]$Mode,
    [Parameter(Mandatory)][string]$Payload,
    [Parameter(Mandatory)][string]$Evidence,
    [Parameter(Mandatory)][string]$Target
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Payload = (Resolve-Path $Payload).Path
$Evidence = (Resolve-Path $Evidence).Path
$contract = Get-Content (Join-Path $Payload 'verification-contract.json') -Raw | ConvertFrom-Json
if ($contract.commit -ne $env:GITHUB_SHA) { throw 'Payload commit does not match workflow commit' }

. (Join-Path $PSScriptRoot 'windows_installer_helpers.ps1')

$gate = @{phase=$Mode;success=$false;commit=$contract.commit}
try {
    if ($Mode -eq 'install') {
        $installer = Join-Path $Payload 'TEMPO_Setup_v1.0.1.exe'
        $expected = (Get-Content (Join-Path $Payload 'SHA256SUMS.txt') -Raw).Trim().Split(' ')[0]
        if ((Get-FileHash $installer -Algorithm SHA256).Hash.ToLower() -ne $expected) { throw 'Installer checksum mismatch' }
        Invoke-Bounded $installer @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',"/DIR=`"$Target`"","/LOG=`"$(Join-Path $Evidence 'installation.log')`"") 300
        if (-not (Test-Path (Join-Path $Target 'TEMPO.exe'))) { throw 'Installer did not create TEMPO.exe' }
        $gate.installer_sha256 = $expected
    } elseif ($Mode -eq 'verify') {
        $profile = Join-Path $Evidence 'empty user profile'
        foreach ($entry in @{LOCALAPPDATA='AppData\Local';APPDATA='AppData\Roaming';USERPROFILE='profile';MPLCONFIGDIR='matplotlib';HF_HOME='huggingface';YOLO_CONFIG_DIR='ultralytics';TORCH_HOME='torch';KERAS_HOME='keras';CELLPOSE_LOCAL_MODELS_PATH='cellpose';NUMBA_CACHE_DIR='numba'}.GetEnumerator()) {
            $directory = Join-Path $profile $entry.Value
            New-Item -ItemType Directory -Force $directory | Out-Null
            [Environment]::SetEnvironmentVariable($entry.Key,$directory,'Process')
        }
        foreach ($name in @('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH')) {
            [Environment]::SetEnvironmentVariable($name,$null,'Process')
        }
        $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
        $env:QT_QPA_PLATFORM='offscreen';$env:MPLBACKEND='Agg';$env:QT_API='pyqt6'
        $env:HF_HUB_OFFLINE='1';$env:HF_HUB_DISABLE_TELEMETRY='1'
        $env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8:backslashreplace'
        $report = Join-Path $Evidence 'installed-verification.json'
        Remove-Item $report -ErrorAction SilentlyContinue
        Invoke-Bounded (Join-Path $Target 'TEMPO.exe') @('--verify-installation',"`"$report`"") 900
        if (-not (Test-Path $report)) { throw 'Installed executable did not write its verification report' }
        Assert-Verification (Get-Content $report -Raw | ConvertFrom-Json) $contract
    } else {
        $uninstaller = Join-Path $Target 'unins000.exe'
        if (-not (Test-Path $uninstaller)) { throw 'Uninstaller is missing' }
        Invoke-Bounded $uninstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$(Join-Path $Evidence 'uninstallation.log')`"") 300
        try {
            Assert-Uninstalled $Target
        } finally {
            $remaining = @(Get-RemainingInstallationFiles $Target | Select-Object FullName,Length)
            ConvertTo-Json -InputObject $remaining -Depth 4 | Set-Content (Join-Path $Evidence 'uninstall-leftovers.json') -Encoding utf8NoBOM
        }
    }
    $gate.success=$true
} catch {
    $gate.error=$_.Exception.Message
    throw
} finally {
    $gate | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $Evidence "$Mode-result.json") -Encoding utf8NoBOM
}
