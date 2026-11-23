"""A detached uninstaller must finish before its installation path is reused."""
import base64
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'win32', 'Requires Windows process and filesystem behavior.')
class WindowsInstallerCleanupTests(unittest.TestCase):
    def run_shell(self, script):
        shell = shutil.which('pwsh.exe')
        self.assertIsNotNone(shell, 'PowerShell 7 is required for Windows installer fixtures.')
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ,
                TEMPO_CLEANUP_TEST_ROOT=str(Path(directory) / 'installation é with spaces'),
                TEMPO_INSTALL_FIXTURE=str(Path(__file__).resolve().parents[1] / 'scripts/verify_velopack_install.ps1'),
                TEMPO_CLEANUP_HELPERS=str(Path(__file__).resolve().parents[1] / 'scripts/windows_installer_helpers.ps1'))
            command = "$ErrorActionPreference='Stop'\n. $env:TEMPO_CLEANUP_HELPERS\n" + script
            encoded = base64.b64encode(command.encode('utf-16le')).decode('ascii')
            result = subprocess.run([shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + '\n' + result.stderr)

    def test_absent_root_is_not_reused_until_detached_cleanup_exits(self):
        self.run_shell(r'''
$target=$env:TEMPO_CLEANUP_TEST_ROOT
New-Item -ItemType Directory $target | Out-Null
$arguments='/D /C choice /C Y /N /D Y /T 3 >nul & rmdir /s /q "' + $target + '"'
$cleanup=Start-Process -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList $arguments -PassThru
$null=$cleanup.Handle
try {
    Remove-Item -LiteralPath $target
    if ($cleanup.WaitForExit(0)) { throw 'Fixture cleanup did not remain pending' }
    Wait-VelopackRemoval $target 15
    if (-not $cleanup.WaitForExit(0)) { throw 'Returned while detached cleanup could still remove a replacement' }
    New-Item -ItemType Directory $target | Out-Null
    Set-Content (Join-Path $target 'replacement.txt') 'replacement remains intact'
} finally {
    if (-not $cleanup.WaitForExit(5000)) { $cleanup.Kill($true); $cleanup.WaitForExit() }
    $cleanup.Dispose()
}
''')

    def test_incomplete_removal_times_out_without_deleting_remaining_files(self):
        self.run_shell(r'''
$target=$env:TEMPO_CLEANUP_TEST_ROOT
New-Item -ItemType Directory $target | Out-Null
$file=Join-Path $target 'preserved.txt'
Set-Content $file 'must remain'
$refused=$false
try { Wait-VelopackRemoval $target 1 }
catch {
    if ($_.Exception.Message -ne 'Velopack uninstall cleanup did not complete before its deadline') { throw }
    $refused=$true
}
if (-not $refused -or (Get-Content $file -Raw).Trim() -ne 'must remain') { throw 'Incomplete uninstall was accepted or changed' }
''')

    def test_exact_directory_rename_refuses_open_descendant_then_succeeds(self):
        self.run_shell(r'''
$target=$env:TEMPO_CLEANUP_TEST_ROOT
$retired=$target + '.retired'
New-Item -ItemType Directory (Join-Path $target 'packages') -Force | Out-Null
$lockPath=Join-Path $target 'packages\.velopack_lock'
$heldLock=[IO.File]::Open($lockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Delete)
try {
    $blocked=$false
    try { [IO.Directory]::Move($target,$retired) }
    catch {
        $cause=$_.Exception
        while ($cause.InnerException) { $cause=$cause.InnerException }
        if ($cause.HResult -ne -2147024891) { throw }
        $blocked=$true
    }
    if (-not $blocked -or -not (Test-Path $target) -or (Test-Path $retired)) {
        throw 'Atomic rename did not fail intact while a descendant was open'
    }
} finally { $heldLock.Dispose() }
[IO.Directory]::Move($target,$retired)
if ((Test-Path $target) -or -not (Test-Path $retired)) { throw 'Atomic rename failed after closing the descendant' }
''')

    def test_fixture_initialization_failure_retains_a_structured_report(self):
        self.run_shell(r'''
$payload=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'payload'
$evidence=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'evidence'
New-Item -ItemType Directory -Force $payload,$evidence | Out-Null
$failed=$false
try { & $env:TEMPO_INSTALL_FIXTURE -Mode gate -Payload $payload -Evidence $evidence -SourceCommit ('a'*40) }
catch { $failed=$true }
$report=Get-Content (Join-Path $evidence 'gate-result.json') -Raw | ConvertFrom-Json
if (-not $failed -or $report.success -ne $false -or $report.source_commit -ne ('a'*40) -or
    $report.error -notlike '*candidate-contract.json*') { throw 'Initialization failure evidence was lost' }
''')

    def test_gate_reports_the_failed_stage_before_a_missing_later_stage(self):
        self.run_shell(r'''
$payload=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'payload'
$evidence=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'evidence'
New-Item -ItemType Directory -Force $payload,$evidence | Out-Null
$repair=Join-Path $payload 'repair.exe'
[IO.File]::WriteAllText($repair,'fixture')
@{schema_version=1;source_commit=('a'*40);release_certified=$false;purpose='isolated-full-app-acceptance';
  package_id='TEMPO.CandidateFixture';title='TEMPO Candidate Fixture';revisions=@(@{},@{},@{});
  repair=@{path='repair.exe';size=(Get-Item $repair).Length;sha256=(Get-FileHash $repair -Algorithm SHA256).Hash.ToLowerInvariant()}} |
  ConvertTo-Json -Depth 8 | Set-Content (Join-Path $payload 'candidate-contract.json') -Encoding utf8NoBOM
@{commit=('a'*40)} | ConvertTo-Json | Set-Content (Join-Path $payload 'verification-contract.json') -Encoding utf8NoBOM
@{success=$false;error='original matrix failure'} | ConvertTo-Json |
  Set-Content (Join-Path $evidence 'matrix-result.json') -Encoding utf8NoBOM
$failed=$false
try { & $env:TEMPO_INSTALL_FIXTURE -Mode gate -Payload $payload -Evidence $evidence -SourceCommit ('a'*40) }
catch { $failed=$true }
$report=Get-Content (Join-Path $evidence 'gate-result.json') -Raw | ConvertFrom-Json
if (-not $failed -or $report.error -notlike '*original matrix failure*') {
    throw 'Later missing evidence obscured the first failing stage'
}
''')

    def test_candidate_gate_requires_matrix_and_uninstall_evidence_only(self):
        self.run_shell(r'''
$payload=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'payload'
$evidence=Join-Path $env:TEMPO_CLEANUP_TEST_ROOT 'evidence'
New-Item -ItemType Directory -Force $payload,$evidence | Out-Null
$repair=Join-Path $payload 'repair.exe'
[IO.File]::WriteAllText($repair,'fixture')
$commit='a'*40
$revisions=@(1..3 | ForEach-Object {
    @{version="1.0.5-test.$_";installer=@{sha256=('b'*64)};full=@{sha256=('c'*64)}}
})
$contract=Join-Path $payload 'candidate-contract.json'
@{schema_version=1;source_commit=$commit;release_certified=$false;purpose='isolated-full-app-acceptance';
  package_id='TEMPO.CandidateFixture';title='TEMPO Candidate Fixture';revisions=$revisions;
  repair=@{path='repair.exe';size=(Get-Item $repair).Length;sha256=(Get-FileHash $repair -Algorithm SHA256).Hash.ToLowerInvariant()}} |
  ConvertTo-Json -Depth 8 | Set-Content $contract -Encoding utf8NoBOM
$identity=@{schema_version=2;commit=$commit;required_checks=@('bundled payload');lock_sha256=('d'*64)}
$identity | ConvertTo-Json | Set-Content (Join-Path $payload 'verification-contract.json') -Encoding utf8NoBOM
$runtime=$identity + @{frozen=$true;success=$true;failures=@();checks=$identity.required_checks;
    runtime_details=@{'bundled payload'=@{identity=@{lock_sha256=$identity.lock_sha256}}}}
foreach ($label in @('fresh','replacement-1','replacement-2','skipped-packaging-revision','offline-repaired','after-repair-next-update')) {
    $runtime | ConvertTo-Json -Depth 8 | Set-Content (Join-Path $evidence "$label-verification.json") -Encoding utf8NoBOM
}
'[]' | Set-Content (Join-Path $evidence 'preserved-state.json') -Encoding utf8NoBOM
$common=@{success=$true;source_commit=$commit;fixture_commit=$env:GITHUB_SHA;release_certified=$false;
    contract_sha256=(Get-FileHash $contract -Algorithm SHA256).Hash.ToLowerInvariant();
    fixture_sha256=(Get-FileHash $env:TEMPO_INSTALL_FIXTURE -Algorithm SHA256).Hash.ToLowerInvariant();
    helper_sha256=(Get-FileHash $env:TEMPO_CLEANUP_HELPERS -Algorithm SHA256).Hash.ToLowerInvariant()}
$scenarios=@{}
foreach ($name in @('full_app_fresh_install','same_source_two_replacements','same_source_skipped_replacement',
    'simulated_missing_current_independent_repair','independent_native_owner_repair','repaired_owner_next_update',
    'immediate_reinstall_preserves_owner','external_state_bytes_preserved','shortcuts_and_registration')) {
    $scenarios[$name]=$true
}
$artifacts=@($revisions | ForEach-Object {
    @{version=$_.version;installer_sha256=$_.installer.sha256;full_package_sha256=$_.full.sha256}
})
$common + @{scenarios=$scenarios;artifacts=$artifacts} | ConvertTo-Json -Depth 8 |
  Set-Content (Join-Path $evidence 'matrix-result.json') -Encoding utf8NoBOM
$reportPath=Join-Path $evidence 'gate-result.json'
$failed=$false
try { & $env:TEMPO_INSTALL_FIXTURE -Mode gate -Payload $payload -Evidence $evidence -SourceCommit $commit }
catch { $failed=$true }
$report=Get-Content $reportPath -Raw | ConvertFrom-Json
if (-not $failed -or $report.error -ne 'Candidate uninstall evidence is missing') {
    throw ('Unexpected gate failure: ' + $report.error)
}
$common + @{scenarios=@{uninstall_preserves_external_state=$true}} | ConvertTo-Json -Depth 8 |
  Set-Content (Join-Path $evidence 'uninstall-result.json') -Encoding utf8NoBOM
& $env:TEMPO_INSTALL_FIXTURE -Mode gate -Payload $payload -Evidence $evidence -SourceCommit $commit
$report=Get-Content $reportPath -Raw | ConvertFrom-Json
if ($report.success -ne $true -or $report.scenarios.uninstall_preserves_external_state -ne $true) {
    throw ('Gate failed with complete matrix and uninstall evidence: ' + $report.error)
}
''')


if __name__ == '__main__':
    unittest.main()
