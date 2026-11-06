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


if __name__ == '__main__':
    unittest.main()
