param(
    [Parameter(Mandatory)][ValidateSet('matrix','migration','uninstall','gate')][string]$Mode,
    [Parameter(Mandatory)][string]$Payload,
    [Parameter(Mandatory)][string]$Evidence,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit = $env:GITHUB_SHA
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force $Evidence | Out-Null
$Payload = (Resolve-Path $Payload).Path
$Evidence = (Resolve-Path $Evidence).Path
. (Join-Path $PSScriptRoot 'windows_installer_helpers.ps1')
$contract = Get-Content (Join-Path $Payload 'candidate-contract.json') -Raw | ConvertFrom-Json
$verification = Get-Content (Join-Path $Payload 'verification-contract.json') -Raw | ConvertFrom-Json
if ($contract.schema_version -ne 1 -or $contract.source_commit -ne $SourceCommit -or
    $verification.commit -ne $SourceCommit -or $contract.release_certified -ne $false -or
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
    if ($config.enabled -ne $false -or @($config.trusted_keys.PSObject.Properties).Count) { throw 'Candidate enabled updates or acquired trust keys' }
    $registered = Get-ItemProperty -LiteralPath $registry
    if ($registered.InstallLocation.TrimEnd('\') -ne $root.TrimEnd('\')) { throw 'Uninstall registration owns a different location' }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($link in $links) {
        if (-not (Test-Path -LiteralPath $link)) { throw "Missing installed shortcut: $link" }
        $shortcut = $shell.CreateShortcut($link)
        if (-not $shortcut.TargetPath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Shortcut points outside the installed application: $link"
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

function Assert-MigrationRefused([string]$Reason, [string[]]$Arguments) {
    $stdout=Join-Path $Evidence "refused-$Reason.log"
    $stderr=Join-Path $Evidence "refused-$Reason-error.log"
    $process=Start-Process -FilePath $repair -ArgumentList $Arguments -WorkingDirectory $Evidence -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $null=$process.Handle
    if (-not $process.WaitForExit(300000)) { throw 'Migration refusal did not settle before its deadline' }
    $process.Refresh()
    if ($process.ExitCode -eq 0 -or (Get-Content $stderr -Raw) -notmatch $Reason) { throw "Migration did not reject $Reason" }
    if (Test-Path (Join-Path $state 'updates\legacy-migration\prepared.json')) { throw 'Unsupported selection was prepared for removal' }
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

$result = @{schema_version=1;source_commit=$contract.source_commit;fixture_commit=$env:GITHUB_SHA;fixture_sha256=(Get-FileHash $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();release_certified=$false;mode=$Mode;success=$false;scenarios=@{};measurements=@()}
try {
    Assert-File $repair $contract.repair
    if ($Mode -eq 'gate') {
        $matrix = Get-Content (Join-Path $Evidence 'matrix-result.json') -Raw | ConvertFrom-Json
        $migration = Get-Content (Join-Path $Evidence 'migration-result.json') -Raw | ConvertFrom-Json
        $uninstall = Get-Content (Join-Path $Evidence 'uninstall-result.json') -Raw | ConvertFrom-Json
        foreach ($report in @($matrix,$migration,$uninstall)) {
            if ($report.success -ne $true -or $report.source_commit -ne $contract.source_commit -or
                $report.fixture_commit -ne $result.fixture_commit -or $report.fixture_sha256 -ne $result.fixture_sha256 -or
                $report.release_certified -ne $false) { throw 'Candidate acceptance failed or has a different identity' }
        }
        foreach ($name in @('full_app_fresh_install','same_source_two_replacements','same_source_skipped_replacement','simulated_missing_current_independent_repair','external_state_bytes_preserved','shortcuts_and_registration')) {
            if ($matrix.scenarios.$name -ne $true) { throw "Candidate scenario is missing: $name" }
        }
        if ($uninstall.scenarios.uninstall_preserves_external_state -ne $true) { throw 'Uninstall preservation evidence is missing' }
        foreach ($name in @('published_legacy_cpu_installer','legacy_data_and_models_preserved','legacy_shortcuts_and_registration_retired','unsupported_components_refused')) {
            if ($migration.scenarios.$name -ne $true) { throw "Legacy migration scenario is missing: $name" }
        }
        if ($migration.legacy_installer_sha256 -ne $contract.legacy_source.installer_sha256) { throw 'Legacy acceptance used a different old installer' }
        if (@($matrix.artifacts).Count -ne 3) { throw 'Exact package acceptance identities are missing' }
        for ($index=0; $index -lt 3; $index++) {
            $observed=$matrix.artifacts[$index]; $expected=$contract.revisions[$index]
            if ($observed.version -ne $expected.version -or $observed.installer_sha256 -ne $expected.installer.sha256 -or
                $observed.full_package_sha256 -ne $expected.full.sha256) { throw 'Acceptance describes different package bytes' }
        }
        foreach ($label in @('fresh','replacement-1','replacement-2','skipped-packaging-revision','offline-repaired','migrated')) {
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
    } elseif ($Mode -eq 'migration') {
        $third=$contract.revisions[2]
        Assert-Installed $third
        $legacy=Join-Path $profile 'old custom installation é'
        $oldInstaller=Join-Path $Evidence 'published-v1.0.1-Setup.exe'
        $ProgressPreference='SilentlyContinue'
        Invoke-WebRequest $contract.legacy_source.url -OutFile $oldInstaller
        if ((Get-FileHash $oldInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.legacy_source.installer_sha256) { throw 'Published legacy installer checksum mismatch' }
        Invoke-Bounded $oldInstaller @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/SP-', '/TASKS=desktopicon',"/DIR=`"$legacy`"","/LOG=`"$(Join-Path $Evidence 'legacy-installation.log')`"") 600
        if ((Get-FileHash (Join-Path $legacy 'TEMPO.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.legacy_source.executable_sha256) { throw 'Actual installed old executable differs from reviewed migration source' }
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
        $arguments=@('--mode','prepare-migration','--legacy-root',"`"$legacy`"",'--legacy-installer',"`"$oldInstaller`"",'--target-manifest',"`"$(Join-Path $Payload $third.manifest.path)`"",'--target-package',"`"$(Join-Path $Payload $third.full.path)`"")
        $receipt=Join-Path $legacy '_internal\.tempo-packages\fixture\receipt.json'
        New-Item -ItemType Directory -Force (Split-Path $receipt) | Out-Null
        [IO.File]::WriteAllText($receipt,'{}')
        Assert-MigrationRefused 'optional package' $arguments
        Remove-Item -LiteralPath $receipt
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
        Invoke-Bounded (Join-Path $legacy 'unins000.exe') @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',"/LOG=`"$(Join-Path $Evidence 'legacy-uninstallation.log')`"") 600
        if (Test-Path (Join-Path $legacy 'TEMPO.exe')) { throw 'Old uninstall left its executable behind' }
        foreach ($link in $oldLinks) { if (Test-Path -LiteralPath $link) { throw "Old uninstall left its shortcut: $link" } }
        # Re-running the exact candidate setup also restores any shared shortcuts.
        Invoke-Bounded (Join-Path $Payload $third.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'migration-setup.log')`"",'--installto',"`"$root`"") 600
        Invoke-Bounded $repair @('--mode','finish-migration') 600
        $draft=Get-Content (Join-Path $state 'collaboration\migration-fixture\draft.json') -Raw | ConvertFrom-Json
        if ($draft.image_path -ne (Join-Path $state 'collaboration\migration-fixture\image.png')) { throw 'Saved image reference was not relocated' }
        $models=@(Get-ChildItem (Join-Path $state 'models') -Recurse -File -Filter 'yolo26s-seg.pt')
        if ($models.Count -ne 1 -or $models[0].Directory.Name -ne $modelRepositoryId -or
            (Get-FileHash $models[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $modelHash) { throw 'Legacy model bytes or saved repository selection were not preserved' }
        if ((Get-FileHash (Join-Path $migrationRoot 'legacy-installer.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.legacy_source.installer_sha256) { throw 'Retained offline legacy installer differs' }
        if (-not (Test-Path (Join-Path $migrationRoot 'completed.json'))) { throw 'Legacy migration did not complete' }
        Assert-Installed $third
        Invoke-Diagnostics 'migrated'
        Assert-State
        $result.legacy_installer_sha256=$contract.legacy_source.installer_sha256
        $result.scenarios=@{published_legacy_cpu_installer=$true;legacy_data_and_models_preserved=$true;legacy_shortcuts_and_registration_retired=$true;unsupported_components_refused=$true}
    } elseif ($Mode -eq 'uninstall') {
        if (Test-Path (Join-Path $root 'Update.exe')) {
            if ((Get-FileHash (Join-Path $root 'Update.exe') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $contract.updater_sha256) { throw 'Uninstall owner checksum mismatch' }
            Invoke-Bounded (Join-Path $root 'Update.exe') @('uninstall','--silent','--log',"`"$(Join-Path $Evidence 'uninstall-native.log')`"",'--rootDir',"`"$root`"") 300
        }
        if (Test-Path (Join-Path $root 'current\TEMPO.exe')) { throw 'Uninstall left the application behind' }
        if (Test-Path -LiteralPath $registry) { throw 'Uninstall left its registration behind' }
        foreach ($link in $links) { if (Test-Path -LiteralPath $link) { throw "Uninstall left a shortcut: $link" } }
        Assert-State
        $result.scenarios.uninstall_preserves_external_state=$true
    } else {
        if ((Test-Path -LiteralPath $root) -or (Test-Path -LiteralPath $registry)) { throw 'Acceptance requires an unused installation identity' }
        foreach ($revision in $contract.revisions) {
            foreach ($asset in @($revision.full,$revision.installer,$revision.manifest)) { Assert-File (Join-Path $Payload $asset.path) $asset }
        }
        # Actual external file formats/locations, with no credentials or research data.
        $preferences=@{
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
        $preserved = @()
        foreach ($entry in $files.GetEnumerator()) {
            $path = Join-Path $profile $entry.Key
            New-Item -ItemType Directory -Force (Split-Path $path) | Out-Null
            [IO.File]::WriteAllText($path,$entry.Value,[Text.UTF8Encoding]::new($false))
            $preserved += @{path=$entry.Key;size=(Get-Item $path).Length;sha256=(Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()}
        }
        ConvertTo-Json -InputObject $preserved -Depth 8 | Set-Content (Join-Path $Evidence 'preserved-state.json') -Encoding utf8NoBOM
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
        Assert-State
        Invoke-Bounded (Join-Path $Payload $first.installer.path) @('--silent','--log',"`"$(Join-Path $Evidence 'skipped-setup.log')`"",'--installto',"`"$root`"") 600
        $result.measurements += Apply-Revision $first $third 'skipped-packaging-revision'
        $result.scenarios.same_source_skipped_replacement=$true
        # Simulate a missing current directory and unavailable native owner. This
        # establishes full-payload independent repair, not a power-loss result.
        Move-Item (Join-Path $root 'current') (Join-Path $root 'fixture-interrupted-current')
        Move-Item (Join-Path $root 'Update.exe') (Join-Path $root 'fixture-unavailable-Update.exe')
        try {
            Invoke-Bounded $repair @('--mode','forward') 600
        } finally {
            Move-Item (Join-Path $root 'fixture-unavailable-Update.exe') (Join-Path $root 'Update.exe')
        }
        Assert-Installed $third
        Invoke-Diagnostics 'offline-repaired'
        Assert-State
        $result.scenarios.simulated_missing_current_independent_repair=$true
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
