function Invoke-Bounded([string]$Executable, [string[]]$Arguments, [int]$Seconds) {
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -WorkingDirectory $Evidence -PassThru
    $null = $process.Handle
    if (-not $process.WaitForExit($Seconds * 1000)) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null
        throw "Process timed out: $Executable"
    }
    $process.Refresh()
    if ($process.ExitCode -ne 0) { throw "Process failed ($($process.ExitCode)): $Executable" }
}

function Assert-Verification($Report, $Contract) {
    if ($Report.schema_version -ne $Contract.schema_version -or $Report.commit -ne $Contract.commit) {
        throw 'Missing or mismatched verification identity'
    }
    if ($Report.frozen -ne $true -or $Report.success -ne $true -or @($Report.failures).Count -ne 0) {
        throw 'Installed executable verification failed'
    }
    $required = @($Contract.required_checks | Sort-Object -Unique)
    foreach ($property in @('checks','required_checks')) {
        $actual = @($Report.$property | Sort-Object -Unique)
        if ($actual.Count -ne $required.Count -or @($Report.$property).Count -ne $required.Count -or
            @(Compare-Object $required $actual).Count -ne 0) { throw 'Required verification checks are incomplete' }
    }
    if (-not $Report.runtime_details) { throw 'Missing runtime evidence' }
    $identity = $Report.runtime_details.'bundled payload'.identity
    if ($identity.lock_sha256 -ne $Contract.lock_sha256) { throw 'Runtime lock identity differs from payload' }
}


function Get-RemainingInstallationFiles([string]$Target) {
    if (-not (Test-Path -LiteralPath $Target)) { return }
    try {
        Get-ChildItem -LiteralPath $Target -Recurse -Force -File -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        # Inno can remove the root between Test-Path and enumeration.
        # Only an absent root establishes successful removal; all other errors fail.
        if (Test-Path -LiteralPath $Target) { throw }
    }
}

function Assert-Uninstalled([string]$Target, [double]$CleanupSeconds = 30) {
    if (Test-Path (Join-Path $Target 'TEMPO.exe')) { throw 'Uninstall left TEMPO.exe behind' }
    if (Test-Path (Join-Path $Target '_internal')) { throw 'Uninstall left the bundled runtime behind' }
    # Inno's running bootstrap deletes itself after the uninstall process exits.
    # Observe that deletion, rather than certifying its transient leftover.
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    while (Test-Path -LiteralPath $Target) {
        $remaining = @(Get-RemainingInstallationFiles $Target)
        if ($remaining.Count -eq 0) { return }
        if (@($remaining | Where-Object { $_.DirectoryName -ne $Target -or $_.Name -notmatch '^unins\d+\.(exe|dat|msg)$' }).Count) {
            throw 'Uninstall left unexpected files behind'
        }
        if ($deadline.Elapsed.TotalSeconds -ge $CleanupSeconds) { throw 'Uninstaller cleanup did not complete before its deadline' }
        Start-Sleep -Milliseconds 100
    }
}
