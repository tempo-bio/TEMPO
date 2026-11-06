"""Exercise the experimental updater on isolated Windows installations.

Reports observed scenarios; it never issues production certification. Windows,
.NET 8, the locked Python dependencies and a fixture-feature updater are needed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def run(*args, env=None, expect=0, timeout=180):
    result = subprocess.run([str(arg) for arg in args], env=env, timeout=timeout,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if (result.returncode == 0) != (expect == 0):
        raise RuntimeError(f'Unexpected exit {result.returncode}: {args}\n{result.stdout}')
    return result


def one(pattern, directory):
    found = list(directory.glob(pattern))
    if len(found) != 1:
        raise RuntimeError(f'Expected one {pattern} in {directory}, found {found}')
    return found[0]


def build_fixture(work, vpk, updater):
    vendor = vpk / 'vendor' / 'update_x64.exe'
    shutil.copyfile(updater, vendor)
    if hashlib.sha256(vendor.read_bytes()).digest() != hashlib.sha256(updater.read_bytes()).digest():
        raise RuntimeError('Packager does not contain the candidate fixture updater')
    dist = work / 'dist'
    run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
        '--name', 'TempoFixture', '--distpath', dist, '--workpath', work / 'build',
        '--specpath', work, HERE / 'app.py', timeout=300)
    payload = dist / 'TempoFixture'
    releases = work / 'releases'
    releases.mkdir()
    dll = vpk / 'tools' / 'net8.0' / 'any' / 'vpk.dll'
    setup = work / 'base-setup.exe'
    for version in ('1.0.1', '1.0.2'):
        (payload / 'fixture-version.txt').write_text(version, encoding='utf-8')
        run('dotnet', dll, '[win]', 'pack', '--packId', 'TEMPO.RecoveryFixture',
            '--packTitle', 'TEMPO Recovery Fixture', '--packVersion', version,
            '--packDir', payload, '--mainExe', 'TempoFixture.exe', '--runtime', 'win-x64',
            '--outputDir', releases, '--noPortable', '--skip-updates', '--yes', '--legacyConsole', timeout=300)
        if version == '1.0.1':
            shutil.copyfile(one('*Setup.exe', releases), setup)
    return setup, one('*1.0.2-full.nupkg', releases)


def probe(root, expected):
    result = run(root / 'current' / 'TempoFixture.exe', '--probe')
    if expected not in result.stdout.splitlines():
        raise AssertionError(f'Wrong installed version: {result.stdout!r}')


def apply(root, package, *, fault=None, abort=None, wait_pid=None, expect_failure=False):
    env = os.environ.copy()
    env.pop('TEMPO_FIXTURE_FAULT', None)
    env.pop('TEMPO_FIXTURE_ABORT', None)
    if fault:
        env['TEMPO_FIXTURE_FAULT'] = fault
    if abort:
        env['TEMPO_FIXTURE_ABORT'] = abort
    args = [root / 'Update.exe', 'apply', '--silent', '--norestart', '--package', package,
            '--rootDir', root, '--packageDir', root / 'packages']
    if wait_pid is not None:
        args.extend(['--waitPid', wait_pid])
    return run(*args, env=env, expect=1 if fault or abort or expect_failure else 0)


def run_scenarios(work, setup, package):
    outcomes = {}
    for scenario in ('normal', 'before_old_rename', 'after_old_rename', 'restore_failure', 'abrupt_after_old', 'running_process', 'wait_timeout'):
        root = work / ('install-' + scenario)
        run(setup, '--silent', '--installto', root, timeout=180)
        probe(root, '1.0.1')
        if scenario == 'normal':
            apply(root, package)
            probe(root, '1.0.2')
            assert (root / 'tempo-previous' / 'TempoFixture.exe').is_file()
            assert (root / 'tempo-update-pending.txt').is_file()
        elif scenario in ('running_process', 'wait_timeout'):
            ready = work / (scenario + '.ready')
            env = os.environ.copy()
            env['TEMPO_FIXTURE_READY'] = str(ready)
            held = subprocess.Popen([str(root / 'current' / 'TempoFixture.exe'), '--hold'], env=env)
            try:
                deadline = time.monotonic() + 30
                while not ready.is_file() and held.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not ready.is_file():
                    raise RuntimeError('Fixture app failed to signal readiness')
                apply(root, package, wait_pid=held.pid if scenario == 'wait_timeout' else None,
                      expect_failure=True)
                if held.poll() is not None:
                    raise AssertionError('Updater terminated the fixture app')
            finally:
                held.terminate()
                held.wait(timeout=10)
            probe(root, '1.0.1')
        elif scenario == 'before_old_rename':
            apply(root, package, fault='before_old_rename')
            probe(root, '1.0.1')
        elif scenario == 'after_old_rename':
            apply(root, package, fault='after_old_rename')
            probe(root, '1.0.1')
            assert (root / 'tempo-update-pending.txt').is_file()
        else:
            if scenario == 'restore_failure':
                apply(root, package, fault='after_old_rename,before_restore')
            else:
                apply(root, package, abort='after_old_rename')
            assert not (root / 'current').exists()
            assert (root / 'tempo-previous' / 'TempoFixture.exe').is_file()
            assert (root / 'tempo-update-pending.txt').is_file()
            # Fixture cleanup only; this is not the required independent repair.
            (root / 'tempo-previous').rename(root / 'current')
            probe(root, '1.0.1')
        outcomes[scenario] = True
        # This root and package ID are fixture-only. Uninstall uses the real updater.
        run(root / 'Update.exe', 'uninstall', '--silent', '--rootDir', root)
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vpk-root', type=Path, required=True)
    parser.add_argument('--fixture-updater', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('The native fixture requires Windows')
    with tempfile.TemporaryDirectory(prefix='tempo-velopack-fixture-') as tmp:
        work = Path(tmp)
        setup, package = build_fixture(work, args.vpk_root.resolve(), args.fixture_updater.resolve())
        outcomes = run_scenarios(work, setup, package)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({'scenarios': outcomes, 'certified': False}, indent=2), encoding='utf-8')
    print('Fixture scenarios passed; production certification still requires independent repair and confirmation tests.')


if __name__ == '__main__':
    main()
