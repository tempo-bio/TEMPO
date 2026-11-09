param(
    [Parameter(Mandatory)][ValidateSet('matrix','migration','uninstall','gate','release-smoke','release-migration')][string]$Mode,
    [Parameter(Mandatory)][string]$Payload,
    [Parameter(Mandatory)][string]$Evidence,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit = $env:GITHUB_SHA,
    [ValidatePattern('^[0-9a-f]{64}$')][string]$ReleaseManifestSha256,
    [switch]$LegacyBridge
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Payload = (Resolve-Path $Payload).Path
$Evidence = (Resolve-Path $Evidence).Path
. (Join-Path $PSScriptRoot 'windows_installer_helpers.ps1')
$result = @{schema_version=1;source_commit=$SourceCommit;fixture_commit=$env:GITHUB_SHA;fixture_sha256=(Get-FileHash $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();helper_sha256=(Get-FileHash (Join-Path $PSScriptRoot 'windows_installer_helpers.ps1') -Algorithm SHA256).Hash.ToLowerInvariant();release_certified=$false;mode=$Mode;success=$false;scenarios=@{};measurements=@()}
try {
$releaseSmoke = $Mode -eq 'release-smoke'
$releaseMigration = $Mode -eq 'release-migration'
$releasePayload = $releaseSmoke -or $releaseMigration
if (($LegacyBridge -or $ReleaseManifestSha256) -and -not $releaseMigration) {
    throw 'Release migration options require release-migration mode'
}
$contractName = if ($releasePayload) { 'release-contract.json' } else { 'candidate-contract.json' }
$contractPath = Join-Path $Payload $contractName
$contract = Get-Content $contractPath -Raw | ConvertFrom-Json
$result.contract_sha256=(Get-FileHash $contractPath -Algorithm SHA256).Hash.ToLowerInvariant()
$verification = Get-Content (Join-Path $Payload 'verification-contract.json') -Raw | ConvertFrom-Json
if ($contract.schema_version -ne 1 -or $contract.source_commit -ne $SourceCommit -or
    $verification.commit -ne $SourceCommit -or $contract.release_certified -ne $false) {
    throw 'Candidate identity/verification contract mismatch'
}
if ($releasePayload) {
    if ($contract.purpose -ne 'stable-prerelease' -or @($contract.revisions).Count -ne 1 -or
        $contract.package_id -ne 'TEMPO.Bio.Desktop' -or $contract.title -ne 'TEMPO' -or
        -not $verification.version -or $verification.version -cne $contract.revisions[0].version) {
        throw 'Expected one version-aligned stable-channel release package'
    }
} elseif (
    $contract.purpose -ne 'isolated-full-app-acceptance' -or @($contract.revisions).Count -ne 3) {
    throw 'Candidate identity/verification contract mismatch'
}
$registry = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$($contract.package_id)"

# The app and all helpers run with disposable state, no checkout or external runtime.
$profile = Join-Path $Evidence 'isolated profil é with spaces'
foreach ($entry in @{LOCALAPPDATA='AppData\Local';APPDATA='AppData\Roaming';USERPROFILE='profile';MPLCONFIGDIR='matplotlib';HF_HOME='huggingface';YOLO_CONFIG_DIR='ultralytics';TORCH_HOME='torch';KERAS_HOME='keras';CELLPOSE_LOCAL_MODELS_PATH='cellpose';NUMBA_CACHE_DIR='numba'}.GetEnumerator()) {
    $directory = Join-Path $profile $entry.Value
    New-Item -ItemType Directory -Force $directory | Out-Null
    [Environment]::SetEnvironmentVariable($entry.Key,$directory,'Process')
}
# Resolve/create shell folders in a fresh child with the installer's environment.
# The parent may have cached paths before USERPROFILE/APPDATA were isolated;
# Velopack's known-folder lookup also requires the directories to exist.
$folderProbe = @'
$ErrorActionPreference = 'Stop'
@{
    desktop = [Environment]::GetFolderPath('DesktopDirectory', 'Create')
    programs = [Environment]::GetFolderPath('Programs', 'Create')
} | ConvertTo-Json -Compress
'@
$encodedProbe = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($folderProbe))
$folderJson = & (Join-Path $PSHOME 'pwsh.exe') -NoLogo -NoProfile -NonInteractive -EncodedCommand $encodedProbe
if ($LASTEXITCODE -ne 0) { throw 'Isolated shell-folder preparation failed' }
$shellFolders = $folderJson | ConvertFrom-Json
foreach ($folder in @($shellFolders.desktop, $shellFolders.programs)) {
    if (-not $folder -or -not (Test-Path -LiteralPath $folder -PathType Container)) { throw 'Isolated shell folder is unavailable' }
}
$shellFolders | ConvertTo-Json | Set-Content (Join-Path $Evidence "$Mode-shell-folders.json") -Encoding utf8NoBOM
$desktop = $shellFolders.desktop
$startPrograms = $shellFolders.programs
$links = @((Join-Path $desktop "$($contract.title).lnk"), (Join-Path $startPrograms "$($contract.title).lnk"))
foreach ($name in @('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH')) {
    [Environment]::SetEnvironmentVariable($name,$null,'Process')
}
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem"
$env:QT_QPA_PLATFORM='offscreen'; $env:MPLBACKEND='Agg'; $env:QT_API='pyqt6'
$env:HF_HUB_OFFLINE='1'; $env:HF_HUB_DISABLE_TELEMETRY='1'
$env:YOLO_OFFLINE='true'; $env:YOLO_AUTOINSTALL='false'
$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8:backslashreplace'
$root = Join-Path $env:LOCALAPPDATA 'TEMPO.Bio.Desktop'
$state = Join-Path $env:LOCALAPPDATA 'TEMPO'
$cache = Join-Path $state 'updates\recovery'
$packages = Join-Path $state 'updates\packages'
$repair = Join-Path $Payload $contract.repair.path
$modelRepo='fixture-lab/models'
$modelRepositoryId=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($modelRepo))).ToLowerInvariant().Substring(0,20)

function Assert-File($Path, $Expected) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        $item.Length -ne $Expected.size -or
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected.sha256) {
        throw "Artifact mismatch: $Path"
    }
}

function Assert-Installed($Revision) {
    $current = Join-Path $root 'current'
    $actual = @(Get-ChildItem -LiteralPath $current -Recurse -Force -File)
    if ($actual.Count -ne @($Revision.installed_inventory).Count) { throw 'Installed file inventory differs' }
    foreach ($entry in $Revision.installed_inventory) { Assert-File (Join-Path $current $entry.path) $entry }
    if ((Get-FileHash (Join-Path $root 'Update.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.updater_sha256) {
        throw 'Installed updater differs from the exact native candidate'
    }
    $config = Get-Content (Join-Path $current '_internal\assets\tempo-updates.json') -Raw | ConvertFrom-Json
    if ($releasePayload) {
        if ($config.enabled -ne $true -or $config.channel -ne 'stable' -or
            $config.native_recovery_certified -ne $true -or $config.updater_sha256 -ne $contract.updater_sha256 -or
            $config.repair_sha256 -ne $contract.repair.sha256 -or -not @($config.trusted_keys.PSObject.Properties).Count) {
            throw 'Installed update trust differs from the reviewed stable-channel configuration'
        }
    } elseif ($config.enabled -ne $false -or @($config.trusted_keys.PSObject.Properties).Count) {
        throw 'Candidate enabled updates or acquired trust keys'
    }
    $registered = Get-ItemProperty -LiteralPath $registry
    if ($registered.InstallLocation.TrimEnd('\') -ne $root.TrimEnd('\')) { throw 'Uninstall registration owns a different location' }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($link in $links) {
        if (-not (Test-Path -LiteralPath $link)) { throw "Missing installed shortcut: $link" }
        $shortcut = $shell.CreateShortcut($link)
        if (-not $shortcut.TargetPath.Equals((Join-Path $current 'TEMPO.exe'), [StringComparison]::OrdinalIgnoreCase) -or
            -not $shortcut.WorkingDirectory.TrimEnd('\').Equals($current.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or
            $shortcut.Arguments) {
            throw "Shortcut does not launch the current application: $link"
        }
    }
}

function Invoke-Diagnostics([string]$Label) {
    $report = Join-Path $Evidence "$Label-verification.json"
    Remove-Item -LiteralPath $report -ErrorAction SilentlyContinue
    Invoke-Bounded (Join-Path $root 'current\TEMPO.exe') @('--verify-installation',"`"$report`"") 900
    Assert-Verification (Get-Content $report -Raw | ConvertFrom-Json) $verification
}

function Assert-State {
    $expected = Get-Content (Join-Path $Evidence 'preserved-state.json') -Raw | ConvertFrom-Json
    foreach ($entry in $expected) { Assert-File (Join-Path $profile $entry.path) $entry }
}

function New-StateFixture {
    $preferences = @{
        capture=@{data_folder=@{value=(Join-Path $profile 'recordings')};mm_adapter_path=@{value='C:\fixture microscope path'}}
        collaboration=@{hf_models_repo=@{value=$modelRepo};collaborator_id=@{value='fixture-lab'}}
    } | ConvertTo-Json -Depth 8
    $files = @{
        'AppData\Local\TEMPO\config\tempo_preferences.json'=$preferences
        'AppData\Local\TEMPO\config\setup_state.json'='{"schema_version":1,"stage":"complete"}'
        'AppData\Local\TEMPO\collaboration\fixture\draft.json'='{"annotations":[{"label":"cell","points":[[1,1],[2,2],[1,2]]}]}'
        'recordings\fixture recording.avi'='synthetic recording bytes'
        'analysis\fixture.csv'="time_ms,amplitude`n0,0`n10,1`n"
    }
    $files["AppData\Local\TEMPO\models\$modelRepositoryId\yolo26n-scout.pt"]='synthetic installed model bytes'
    $preserved = foreach ($entry in $files.GetEnumerator()) {
        $path = Join-Path $profile $entry.Key
        New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
        [IO.File]::WriteAllText($path,$entry.Value,[Text.UTF8Encoding]::new($false))
        @{path=$entry.Key;size=(Get-Item $path).Length;sha256=(Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()}
    }
    ConvertTo-Json -InputObject @($preserved) -Depth 8 | Set-Content (Join-Path $Evidence 'preserved-state.json') -Encoding utf8NoBOM
}

function Assert-VelopackUninstalled {
    Wait-VelopackRemoval $root
    if (Test-Path -LiteralPath $registry) { throw 'Uninstall left its registration behind' }
    foreach ($link in $links) { if (Test-Path -LiteralPath $link) { throw "Uninstall left a shortcut: $link" } }
    Assert-State
}

function Assert-MigrationRefused([string]$Reason, [string[]]$Arguments, [bool]$Prepared = $false) {
    $preparedPath=Join-Path $state 'updates\legacy-migration\prepared.json'
    if ((Test-Path -LiteralPath $preparedPath) -ne $Prepared) { throw 'Unexpected migration state before refusal check' }
    $before=if ($Prepared) { (Get-FileHash -LiteralPath $preparedPath -Algorithm SHA256).Hash } else { $null }
    $stdout=Join-Path $Evidence "refused-$Reason.log"
    $stderr=Join-Path $Evidence "refused-$Reason-error.log"
    $process=Start-Process -FilePath $repair -ArgumentList $Arguments -WorkingDirectory $Evidence -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $null=$process.Handle
    try {
        if (-not $process.WaitForExit(300000)) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
            throw 'Migration refusal did not settle before its deadline'
        }
        $process.Refresh()
        if ($process.ExitCode -eq 0 -or (Get-Content $stderr -Raw) -notmatch $Reason) { throw "Migration did not reject $Reason" }
    } finally { $process.Dispose() }
    if ((Test-Path -LiteralPath $preparedPath) -ne $Prepared -or
        ($Prepared -and (Get-FileHash -LiteralPath $preparedPath -Algorithm SHA256).Hash -ne $before)) { throw 'Refused migration changed preparation state' }
    Assert-State
}

function Arm-Recovery($Base, $Target) {
    if (Test-Path -LiteralPath $cache) {
        # Disposable test journals are retained under evidence, never overwritten.
        Move-Item -LiteralPath $cache -Destination (Join-Path $Evidence ("confirmed-recovery-" + [guid]::NewGuid().ToString('N')))
    }
    New-Item -ItemType Directory -Force $cache,$packages | Out-Null
    foreach ($entry in @(@{name='base';revision=$Base},@{name='target';revision=$Target})) {
        Copy-Item -LiteralPath (Join-Path $Payload $entry.revision.full.path) -Destination (Join-Path $cache "$($entry.name).nupkg")
        Copy-Item -LiteralPath (Join-Path $Payload $entry.revision.manifest.path) -Destination (Join-Path $cache "$($entry.name).json")
    }
    @{
        format='tempo-update-recovery-v1'; root=$root; packages_dir=$packages; channel='prerelease'
        base=@($Base.version,$contract.source_commit,1); target=@($Target.version,$contract.source_commit,1)
        base_sha256=$Base.full.sha256; target_sha256=$Target.full.sha256; nvidia_marker=$null; state='armed'
    } | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $cache 'recovery.json') -Encoding utf8NoBOM
}

function Apply-Revision($Base, $Target, [string]$Label) {
    Assert-Installed $Base
    Arm-Recovery $Base $Target
    $timer = [Diagnostics.Stopwatch]::StartNew()
    Invoke-Bounded (Join-Path $root 'Update.exe') @('apply','--silent','--log',"`"$(Join-Path $Evidence "$Label-native.log")`"",'--norestart','--package',"`"$(Join-Path $Payload $Target.full.path)`"",'--rootDir',"`"$root`"",'--packageDir',"`"$packages`"") 600
    $seconds = $timer.Elapsed.TotalSeconds
    Assert-Installed $Target
    Invoke-Diagnostics $Label
    Assert-State
    # This confirms observed diagnostic health, not physical hardware/desktop readiness.
    Invoke-Bounded $repair @('--mode','confirm','--version',$Target.version,'--commit',$contract.source_commit) 300
    if (Test-Path (Join-Path $root 'tempo-previous')) { throw 'Confirmation did not retire the verified previous payload' }
    if (Test-Path (Join-Path $root 'tempo-update-pending.txt')) { throw 'Confirmation left native pending state' }
    return @{label=$Label;seconds=$seconds;full_bytes=$Target.full.size;expanded_bytes=$Target.expanded_bytes}
}

    Assert-File $repair $contract.repair
    if ($Mode -eq 'gate') {
        $reports=@{}
        foreach ($stage in @('matrix','migration','uninstall')) {
            $path=Join-Path $Evidence "$stage-result.json"
            if (-not (Test-Path -LiteralPath $path)) { throw "Candidate $stage evidence is missing" }
            $report=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            if ($report.success -ne $true -or $report.source_commit -ne $contract.source_commit -or
                $report.contract_sha256 -ne $result.contract_sha256 -or
                $report.fixture_commit -ne $result.fixture_commit -or $report.fixture_sha256 -ne $result.fixture_sha256 -or
                $report.helper_sha256 -ne $result.helper_sha256 -or
                $report.release_certified -ne $false) { throw "Candidate $stage acceptance failed or has a different identity: $($report.error)" }
            $reports[$stage]=$report
        }
        $matrix=$reports.matrix; $migration=$reports.migration; $uninstall=$reports.uninstall
        foreach ($name in @('full_app_fresh_install','same_source_two_replacements','same_source_skipped_replacement','simulated_missing_current_independent_repair','independent_native_owner_repair','repaired_owner_next_update','immediate_reinstall_preserves_owner','external_state_bytes_preserved','shortcuts_and_registration')) {
            if ($matrix.scenarios.$name -ne $true) { throw "Candidate scenario is missing: $name" }
        }
        if ($uninstall.scenarios.uninstall_preserves_external_state -ne $true) { throw 'Uninstall preservation evidence is missing' }
        foreach ($name in @('published_legacy_cpu_installer','legacy_data_and_models_preserved','legacy_shortcuts_and_registration_retired','unsupported_components_refused','finish_before_legacy_retirement_refused')) {
            if ($migration.scenarios.$name -ne $true) { throw "Legacy migration scenario is missing: $name" }
        }
        if ($migration.legacy_installer_sha256 -ne $contract.legacy_source.installer_sha256) { throw 'Legacy acceptance used a different old installer' }
        if (@($matrix.artifacts).Count -ne 3) { throw 'Exact package acceptance identities are missing' }
        for ($index=0; $index -lt 3; $index++) {
            $observed=$matrix.artifacts[$index]; $expected=$contract.revisions[$index]
            if ($observed.version -ne $expected.version -or $observed.installer_sha256 -ne $expected.installer.sha256 -or
                $observed.full_package_sha256 -ne $expected.full.sha256) { throw 'Acceptance describes different package bytes' }
        }
        foreach ($label in @('fresh','replacement-1','replacement-2','skipped-packaging-revision','offline-repaired','after-repair-next-update','migrated')) {
            Assert-Verification (Get-Content (Join-Path $Evidence "$label-verification.json") -Raw | ConvertFrom-Json) $verification
        }
        Assert-State
        $result.contract_sha256=(Get-FileHash (Join-Path $Payload 'candidate-contract.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        $result.artifacts=$matrix.artifacts
        $result.limits=$matrix.limits
        $result.scenarios=@{}
        foreach ($property in $matrix.scenarios.PSObject.Properties) { $result.scenarios[$property.Name]=$property.Value }
        foreach ($property in $migration.scenarios.PSObject.Properties) { $result.scenarios[$property.Name]=$property.Value }
        $result.scenarios.uninstall_preserves_external_state=$true
        $result.legacy_installer_sha256=$migration.legacy_installer_sha256
    } elseif ($releaseSmoke) {
        foreach ($path in @($root,$registry) + $links) {
            if (Test-Path -LiteralPath $path) { throw "Use a clean Windows profile for release verification: $path" }
        }
        $revision = $contract.revisions[0]
        $installer = Join-Path $Payload $revision.installer.path
        Assert-File $installer $revision.installer
        Assert-File (Join-Path $Payload $revision.full.path) $revision.full
        $stateFile = 'AppData\Local\TEMPO\release-smoke-state.txt'
        $statePath = Join-Path $profile $stateFile
        New-Item -ItemType Directory -Force $state | Out-Null
        [IO.File]::WriteAllText($statePath, 'retain this release fixture')
        ConvertTo-Json -InputObject @(@{path=$stateFile;size=(Get-Item $statePath).Length;
            sha256=(Get-FileHash -LiteralPath $statePath).Hash.ToLowerInvariant()}) |
            Set-Content (Join-Path $Evidence 'preserved-state.json') -Encoding utf8NoBOM
        Invoke-Bounded $installer @('--silent','--log',"`"$(Join-Path $Evidence 'release-setup.log')`"",
            '--installto',"`"$root`"") 600
        Assert-Installed $revision
        Invoke-Diagnostics 'release-installed'
        Invoke-Bounded (Join-Path $root 'current\TEMPORepair.exe') @('--help') 120
        Assert-State
        Invoke-Bounded (Join-Path $root 'Update.exe') @('uninstall','--silent','--log',
            "`"$(Join-Path $Evidence 'release-uninstall.log')`"",'--rootDir',"`"$root`"") 300
        Assert-VelopackUninstalled
        $result.artifacts = @{version=$revision.version;installer_sha256=$revision.installer.sha256;
            full_package_sha256=$revision.full.sha256;repair_sha256=$contract.repair.sha256;
            updater_sha256=$contract.updater_sha256}
        $result.scenarios = @{fresh_install=$true;frozen_runtime=$true;repair_entrypoint=$true;
            uninstall_preserves_external_state=$true}
    } elseif ($Mode -eq 'migration' -or $releaseMigration) {
        if ($releaseMigration) {
            foreach ($path in @($root,$registry) + $links) {
                if (Test-Path -LiteralPath $path) { throw "Use a clean Windows profile for release migration: $path" }
            }
            $target = $contract.revisions[0]
            $manifestPath = Join-Path $Payload "tempo-update-v$($target.version).json"
            if (-not $ReleaseManifestSha256 -or (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $ReleaseManifestSha256) {
                throw 'Published release manifest checksum mismatch'
            }
            foreach ($asset in @($target.full,$target.installer)) { Assert-File (Join-Path $Payload $asset.path) $asset }
            $legacySource = @{
                installer_sha256='5f462548ef2c086140c91b207c6b67679f45b72f35f4432dee727107536381a4'
                executable_sha256='f9daa9029e7949ec052853abe6c0d2d59c664c2b36e1fc2155f59913e5fdb030'
            }
            New-StateFixture
            Invoke-Bounded (Join-Path $Payload $target.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'release-setup.log')`"",'--installto',"`"$root`"") 600
        } else {
            $target = $contract.revisions[2]
            $legacySource = $contract.legacy_source
            $manifestPath = Join-Path $Payload $target.manifest.path
        }
        Assert-Installed $target
        $legacy=Join-Path $profile 'old custom installation é'
        if ($releaseMigration) {
            $oldInstaller = Join-Path $Payload 'TEMPO_Setup_v1.0.1.exe'
        } else {
            $oldInstaller = Join-Path $Evidence 'published-v1.0.1-Setup.exe'
            $ProgressPreference = 'SilentlyContinue'
            Invoke-WebRequest $legacySource.url -OutFile $oldInstaller -MaximumRetryCount 3 -RetryIntervalSec 5
        }
        if ((Get-FileHash $oldInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $legacySource.installer_sha256) { throw 'Published legacy installer checksum mismatch' }
        $legacySetupArgs = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-', '/TASKS=desktopicon',"/DIR=`"$legacy`"")
        if ($LegacyBridge) {
            $bridgeInstaller = Join-Path $Payload 'TEMPO_Setup_v1.0.0.exe'
            $result.bridge_installer_sha256 = (Get-FileHash $bridgeInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($result.bridge_installer_sha256 -cne '5e2e94e0a9323ff88606fc31032a7f2aa95d89bb675750784d69d02be1dc27f4') {
                throw 'Published v1.0.0 installer checksum mismatch'
            }
            Invoke-Bounded $bridgeInstaller ($legacySetupArgs + "/LOG=`"$(Join-Path $Evidence 'bridge-installation.log')`"") 600
            $bridgeFile = Join-Path $legacy '_internal\datasets\bridge-fixture\sample.txt'
            New-Item -ItemType Directory -Force (Split-Path $bridgeFile) | Out-Null
            Set-Content -LiteralPath $bridgeFile -Value 'v1.0.0 test data' -Encoding utf8NoBOM
            $bridgeHash = (Get-FileHash $bridgeFile -Algorithm SHA256).Hash
        }
        Invoke-Bounded $oldInstaller ($legacySetupArgs + "/LOG=`"$(Join-Path $Evidence 'legacy-installation.log')`"") 600
        if ((Get-FileHash (Join-Path $legacy 'TEMPO.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $legacySource.executable_sha256) { throw 'Actual installed old executable differs from reviewed migration source' }
        if ($LegacyBridge -and (Get-FileHash $bridgeFile -Algorithm SHA256).Hash -ne $bridgeHash) { throw 'v1.0.1 did not preserve the v1.0.0 test data' }
        $oldLinks=@((Join-Path $desktop 'TEMPO.lnk'),(Join-Path $startPrograms 'TEMPO\TEMPO.lnk'))
        foreach ($link in $oldLinks) { if (-not (Test-Path -LiteralPath $link)) { throw "Old installer did not create its shortcut: $link" } }
        $data=Join-Path $legacy '_internal\datasets'
        New-Item -ItemType Directory -Force (Join-Path $data 'fixture'),(Join-Path $data 'migration-fixture') | Out-Null
        Copy-Item (Join-Path $state 'collaboration\fixture\draft.json') (Join-Path $data 'fixture\draft.json')
        $image=Join-Path $data 'migration-fixture\image.png'
        [IO.File]::WriteAllBytes($image,[byte[]]@(137,80,78,71))
        @{image_path=$image;annotations=@(@{label='cell';points=@(@(1,1),@(2,2),@(1,2))})} | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $data 'migration-fixture\draft.json') -Encoding utf8NoBOM
        $model=Join-Path $legacy '_internal\yolo26s-seg.pt'
        if (Test-Path $model) { throw 'Synthetic fixture would overwrite a bundled legacy model' }
        [IO.File]::WriteAllText($model,'synthetic legacy model',[Text.UTF8Encoding]::new($false))
        $modelHash=(Get-FileHash $model -Algorithm SHA256).Hash.ToLowerInvariant()
        $arguments=@('--mode','prepare-migration','--legacy-root',"`"$legacy`"",'--legacy-installer',"`"$oldInstaller`"",'--target-manifest',"`"$manifestPath`"",'--target-package',"`"$(Join-Path $Payload $target.full.path)`"")
        $packageRoot=Join-Path $legacy '_internal\.tempo-packages'
        if (Test-Path -LiteralPath $packageRoot) { throw 'Synthetic package fixture would overlap existing package recovery material' }
        $receipt=Join-Path $packageRoot 'fixture\receipt.json'
        New-Item -ItemType Directory -Force (Split-Path $receipt) | Out-Null
        [IO.File]::WriteAllText($receipt,'{}')
        Assert-MigrationRefused 'optional package' $arguments
        Remove-Item -LiteralPath $receipt
        # Remove only the empty directories this fixture created. An empty
        # historical package tree is itself a migration blocker.
        Remove-Item -LiteralPath (Split-Path $receipt)
        Remove-Item -LiteralPath $packageRoot
        $marker=Join-Path $state 'nvidia-runtime\active.json'
        New-Item -ItemType Directory -Force (Split-Path $marker) | Out-Null
        [IO.File]::WriteAllText($marker,'{"fixture":"unreviewed selection"}')
        Assert-MigrationRefused 'NVIDIA' $arguments
        if ((Get-Content $marker -Raw) -ne '{"fixture":"unreviewed selection"}') { throw 'Refused NVIDIA migration changed its selection' }
        Remove-Item -LiteralPath $marker
        Invoke-Bounded $repair $arguments 600
        $migrationRoot=Join-Path $state 'updates\legacy-migration'
        if (-not (Test-Path (Join-Path $migrationRoot 'prepared.json'))) { throw 'Legacy migration was not prepared' }
        if (Test-Path (Join-Path $state 'config\collaboration-location.json')) { throw 'Preparation prematurely published collaboration storage' }
        Assert-MigrationRefused 'still registered' @('--mode','finish-migration') $true
        Invoke-Bounded (Join-Path $legacy 'unins000.exe') @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$(Join-Path $Evidence 'legacy-uninstallation.log')`"") 600
        if (Test-Path (Join-Path $legacy 'TEMPO.exe')) { throw 'Old uninstall left its executable behind' }
        foreach ($link in $oldLinks) { if (Test-Path -LiteralPath $link) { throw "Old uninstall left its shortcut: $link" } }
        # Re-running the exact candidate setup also restores any shared shortcuts.
        Invoke-Bounded (Join-Path $Payload $target.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'migration-setup.log')`"",'--installto',"`"$root`"") 600
        Invoke-Bounded $repair @('--mode','finish-migration') 600
        $draft=Get-Content (Join-Path $state 'collaboration\migration-fixture\draft.json') -Raw | ConvertFrom-Json
        if ($draft.image_path -ne (Join-Path $state 'collaboration\migration-fixture\image.png')) { throw 'Saved image reference was not relocated' }
        $models=@(Get-ChildItem (Join-Path $state 'models') -Recurse -File -Filter 'yolo26s-seg.pt')
        if ($models.Count -ne 1 -or $models[0].Directory.Name -ne $modelRepositoryId -or
            (Get-FileHash $models[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $modelHash) { throw 'Legacy model bytes or saved repository selection were not preserved' }
        if ((Get-FileHash (Join-Path $migrationRoot 'legacy-installer.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $legacySource.installer_sha256) { throw 'Retained offline legacy installer differs' }
        if (-not (Test-Path (Join-Path $migrationRoot 'completed.json'))) { throw 'Legacy migration did not complete' }
        Assert-Installed $target
        Invoke-Diagnostics 'migrated'
        Assert-State
        $result.legacy_installer_sha256=$legacySource.installer_sha256
        $result.scenarios=@{published_legacy_cpu_installer=$true;legacy_data_and_models_preserved=$true;legacy_shortcuts_and_registration_retired=$true;unsupported_components_refused=$true;finish_before_legacy_retirement_refused=$true}
        if ($LegacyBridge) {
            $migratedBridgeFile = Join-Path $state 'collaboration\bridge-fixture\sample.txt'
            if ((Get-FileHash $migratedBridgeFile -Algorithm SHA256).Hash -ne $bridgeHash) { throw 'v1.0.2 did not preserve the v1.0.0 test data' }
            $result.scenarios.older_installer_bridge_preserved_data = $true
        }
        if ($releaseMigration) {
            $result.artifacts = @{version=$target.version;installer_sha256=$target.installer.sha256;
                full_package_sha256=$target.full.sha256;repair_sha256=$contract.repair.sha256;
                updater_sha256=$contract.updater_sha256;manifest_sha256=$ReleaseManifestSha256}
        }
    } elseif ($Mode -eq 'uninstall') {
        if (Test-Path (Join-Path $root 'Update.exe')) {
            if ((Get-FileHash (Join-Path $root 'Update.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.updater_sha256) { throw 'Uninstall owner checksum mismatch' }
            Invoke-Bounded (Join-Path $root 'Update.exe') @('uninstall','--silent','--log',"`"$(Join-Path $Evidence 'uninstall-native.log')`"",'--rootDir',"`"$root`"") 300
        }
        Assert-VelopackUninstalled
        $result.scenarios.uninstall_preserves_external_state=$true
    } else {
        if ((Test-Path -LiteralPath $root) -or (Test-Path -LiteralPath $registry)) { throw 'Acceptance requires an unused installation identity' }
        foreach ($revision in $contract.revisions) {
            foreach ($asset in @($revision.full,$revision.installer,$revision.manifest)) { Assert-File (Join-Path $Payload $asset.path) $asset }
        }
        New-StateFixture
        $first,$second,$third = $contract.revisions
        Invoke-Bounded (Join-Path $Payload $first.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'fresh-setup.log')`"",'--installto',"`"$root`"") 600
        Assert-Installed $first
        Invoke-Diagnostics 'fresh'
        Assert-State
        $result.scenarios.full_app_fresh_install=$true
        $result.measurements += Apply-Revision $first $second 'replacement-1'
        $result.measurements += Apply-Revision $second $third 'replacement-2'
        $result.scenarios.same_source_two_replacements=$true
        # Use the exact first installer again; do not manufacture a prior base.
        Invoke-Bounded (Join-Path $root 'Update.exe') @('uninstall','--silent','--log',"`"$(Join-Path $Evidence 'before-skipped-uninstall.log')`"",'--rootDir',"`"$root`"") 300
        Assert-VelopackUninstalled
        Invoke-Bounded (Join-Path $Payload $first.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'skipped-setup.log')`"",'--installto',"`"$root`"") 600
        Assert-Installed $first
        $result.scenarios.immediate_reinstall_preserves_owner=$true
        # Repair into the second revision so the next real native update can be
        # exercised. No updater is manually copied back after either repair.
        Arm-Recovery $first $second
        Move-Item (Join-Path $root 'current') (Join-Path $root 'fixture-interrupted-current')
        Remove-Item -LiteralPath (Join-Path $root 'Update.exe')
        [IO.File]::WriteAllText((Join-Path $root 'tempo-update-pending.txt'),'damaged fixture marker')
        Invoke-Bounded $repair @('--mode','forward') 600
        Assert-Installed $second
        [IO.File]::WriteAllText((Join-Path $root 'Update.exe'),'damaged fixture owner; never execute')
        Invoke-Bounded $repair @('--mode','forward') 600
        Assert-Installed $second
        $retainedOwners=@(Get-ChildItem -LiteralPath $root -Filter 'tempo-repair-owner-quarantine-*' -File)
        if ($retainedOwners.Count -ne 1 -or (Get-Content $retainedOwners[0].FullName -Raw) -ne 'damaged fixture owner; never execute') { throw 'Repair did not retain the damaged native owner' }
        Invoke-Diagnostics 'offline-repaired'
        Assert-State
        $result.scenarios.simulated_missing_current_independent_repair=$true
        $result.scenarios.independent_native_owner_repair=$true
        Invoke-Bounded $repair @('--mode','confirm','--version',$second.version,'--commit',$contract.source_commit) 300
        $result.measurements += Apply-Revision $second $third 'after-repair-next-update'
        $result.scenarios.repaired_owner_next_update=$true
        Invoke-Bounded (Join-Path $root 'Update.exe') @('uninstall','--silent','--log',"`"$(Join-Path $Evidence 'before-skipped-final-uninstall.log')`"",'--rootDir',"`"$root`"") 300
        Assert-VelopackUninstalled
        Invoke-Bounded (Join-Path $Payload $first.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'skipped-final-setup.log')`"",'--installto',"`"$root`"") 600
        $result.measurements += Apply-Revision $first $third 'skipped-packaging-revision'
        $result.scenarios.same_source_skipped_replacement=$true
        $result.scenarios.external_state_bytes_preserved=$true
        $result.scenarios.shortcuts_and_registration=$true
        $result.artifacts=@($contract.revisions | ForEach-Object { @{version=$_.version;installer_sha256=$_.installer.sha256;full_package_sha256=$_.full.sha256} })
        $result.limits=@('same source in all packaging revisions','diagnostic entrypoints; interactive desktop not accepted','no physical camera, stimulation, or GPU acceptance','no HTTP update selection/download transaction','no power-loss injection','legacy migration limited to reviewed v1.0.1 CPU; NVIDIA and add-ons refused','fixture bytes preserved; saved state not loaded by desktop')
    }
    $result.success=$true
} catch {
    $result.error=$_.Exception.Message
    throw
} finally {
    $result | ConvertTo-Json -Depth 12 | Set-Content (Join-Path $Evidence "$Mode-result.json") -Encoding utf8NoBOM
}
