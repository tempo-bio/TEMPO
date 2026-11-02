param(
    [Parameter(Mandatory=$true)][string]$WorkDirectory,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$WorkDirectory=[IO.Path]::GetFullPath($WorkDirectory)
$OutputDirectory=[IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $WorkDirectory) { throw 'Choose a new working folder for package verification.' }
New-Item -ItemType Directory -Path $WorkDirectory | Out-Null
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$installer=Join-Path $WorkDirectory 'TEMPO_Setup_v1.0.1.exe'
Invoke-WebRequest 'https://github.com/tempo-bio/TEMPO/releases/download/v1.0.1/TEMPO_Setup_v1.0.1.exe' -OutFile $installer
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne '5f462548ef2c086140c91b207c6b67679f45b72f35f4432dee727107536381a4') { throw 'The stable installer checksum differs.' }
$base=Join-Path $WorkDirectory 'TEMPO'
$process=Start-Process -FilePath $installer -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-',('/DIR="'+$base+'"')) -PassThru
if (-not $process.WaitForExit(300000)) { $process.Kill(); throw 'Base installation timed out.' }
$process.Refresh()
if ($process.ExitCode -ne 0) { throw 'Base installation failed.' }
$source=Join-Path $OutputDirectory 'TEMPO_Stimulation_Investigation_1_v0.1.0_source.zip'
python -m scripts.package_stimulation_source --output $source
if ($LASTEXITCODE -ne 0) { throw 'Source archive creation failed.' }
python -m scripts.package_stimulation_investigation --installed $base --source . --source-archive $source --output $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'Package creation failed.' }
$package=Join-Path $OutputDirectory 'TEMPO_Stimulation_Investigation_1_v0.1.0.zip'
$unpacked=Join-Path $WorkDirectory 'package'
Expand-Archive -LiteralPath $package -DestinationPath $unpacked
$evidence=Join-Path $OutputDirectory 'evidence'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/test_stimulation_package_install.ps1 -Package $unpacked -TempoDirectory $base -Evidence $evidence
if ($LASTEXITCODE -ne 0) { throw 'Installed-package verification failed.' }
Write-Output 'Package installation, collection, removal, and reinstall verified.'
