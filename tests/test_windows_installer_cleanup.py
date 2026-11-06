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
if (-not $failed -or $report.error -notlike '*original matrix failure*' -or $report.error -like '*migration-result*') {
    throw 'Later missing evidence obscured the first failing stage'
}
''')


if __name__ == '__main__':
    unittest.main()
