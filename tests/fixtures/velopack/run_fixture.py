"""Exercise the experimental updater on isolated Windows installations.

Reports observed scenarios; it never issues production certification. Windows,
.NET 8, the locked Python dependencies and a fixture-feature updater are needed.
"""
import argparse
import base64
from datetime import datetime, timezone
import zipfile
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import tempo_update_manifest as manifest
import tempo_update_recovery as recovery


def signed_release(package, version, key, keys):
    inventory = []
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            if not item.is_dir():
                data = archive.read(item)
                inventory.append({'path': item.filename, 'size': len(data),
                                  'sha256': hashlib.sha256(data).hexdigest()})
    content = package.read_bytes()
    obj = {'schema_version': 1, 'package_id': manifest.PACKAGE_ID, 'version': version,
           'commit': {'1.0.1': 'a', '1.0.2': 'b', '1.0.3': 'c'}[version] * 40,
           'architecture': 'win-x64', 'channel': 'stable', 'sequence': 1,
           'expires_at': '2099-01-01T00:00:00Z',
           'full': {'name': package.name,
                    'url': 'https://github.com/tempo-bio/TEMPO/releases/download/v' + version + '/' + package.name,
                    'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()},
           'inventory': inventory, 'patches': [], 'minimum_updater_version': '1.2.161',
           'storage_schema_version': 1}
    signed = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    envelope = json.dumps({'signed': obj, 'signatures': [{'key_id': 'fixture',
        'signature': base64.b64encode(key.sign(signed)).decode()}]}).encode()
    return manifest.load_verified_manifest(envelope, keys, now=datetime.now(timezone.utc),
                                           min_sequence=0, expected_channel='stable')



def run(*args, env=None, expect=0, timeout=180):
    result = subprocess.run([str(arg) for arg in args], env=env, timeout=timeout,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if expect is not None and (result.returncode == 0) != (expect == 0):
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
        '--name', 'TEMPO', '--paths', PROJECT, '--distpath', dist, '--workpath', work / 'build',
        '--specpath', work, HERE / 'app.py', timeout=300)
    payload = dist / 'TEMPO'
    releases = work / 'releases'
    releases.mkdir()
    dll = vpk / 'tools' / 'net8.0' / 'any' / 'vpk.dll'
    setup = work / 'base-setup.exe'
    for version in ('1.0.1', '1.0.2', '1.0.3'):
        (payload / 'fixture-version.txt').write_text(version, encoding='utf-8')
        run('dotnet', dll, '[win]', 'pack', '--packId', 'TEMPO.RecoveryFixture',
            '--packTitle', 'TEMPO Recovery Fixture', '--packVersion', version,
            '--packDir', payload, '--mainExe', 'TEMPO.exe', '--runtime', 'win-x64',
            '--outputDir', releases, '--noPortable', '--skip-updates', '--yes', '--legacyConsole', timeout=300)
        if version == '1.0.1':
            shutil.copyfile(one('*Setup.exe', releases), setup)
    return setup, {v: one('*' + v + '-full.nupkg', releases) for v in ('1.0.1', '1.0.2', '1.0.3')}


def probe(root, expected, env):
    result = run(root / 'current' / 'TEMPO.exe', '--probe', env=env)
    if expected not in result.stdout.splitlines():
        raise AssertionError(f'Wrong installed version: {result.stdout!r}')


def apply(root, package, *, env, fault=None, abort=None, wait_pid=None, expect_failure=False, inspect_exit=False):
    env = dict(env)
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
    expect = None if inspect_exit else (1 if fault or abort or expect_failure else 0)
    return run(*args, env=env, expect=expect)


def fixture_process_running(pid, *, wait_ms=0):
    """Observe a synthetic hook child without terminating it."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # PID no longer exists
            return False
        raise ctypes.WinError(error)
    try:
        result = kernel.WaitForSingleObject(handle, wait_ms)
        if result not in (0, 258):  # WAIT_OBJECT_0 / WAIT_TIMEOUT
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 258
    finally:
        kernel.CloseHandle(handle)


def assert_running_app_refused(result, root, *, app_alive):
    if not app_alive:
        raise AssertionError('Updater terminated the live fixture app.\n' + result.stdout)
    if result.returncode == 0:
        raise AssertionError('Updater accepted an update with a live fixture app.\n' + result.stdout)
    if (root / 'current' / 'fixture-version.txt').read_text().strip() != '1.0.1':
        raise AssertionError('Refused update changed the installed application')
    if (root / 'tempo-previous').exists() or (root / 'tempo-update-pending.txt').exists():
        raise AssertionError('Refused update began replacing the installed application')


def run_scenarios(work, setup, packages, repair, releases, keys, outcomes, report):
    package = packages['1.0.2']
    for scenario in ('normal', 'guarded_handoff', 'consecutive', 'before_old_rename', 'after_old_rename', 'restore_failure', 'abrupt_after_old', 'running_process', 'uninstall_running_process', 'uninstall_rename_blocked', 'uninstall_reinstall', 'hook_cleanup', 'legacy_writer_guard', 'wait_timeout'):
        suffix = ' é %TEMPO_UNINSTALL_LITERAL%! &' if scenario == 'uninstall_reinstall' else ''
        local = work / ('profile-' + scenario + suffix)
        local.mkdir()
        root = local / 'TEMPO.Bio.Desktop'
        lock = local / 'TEMPO' / 'updates' / 'packages'
        cache = local / 'TEMPO' / 'updates' / 'recovery'
        env = os.environ.copy()
        env['LOCALAPPDATA'] = str(local)
        if scenario == 'uninstall_reinstall':
            env['TEMPO_UNINSTALL_LITERAL'] = 'must-not-expand-in-a-path'
        def arm(base, target):
            return recovery.arm_recovery(root, cache, releases[base], packages[base],
                                         releases[target], packages[target], trusted_keys=keys,
                                         packages_dir=lock, expected_root=root)
        def repair_mode(mode):
            run(repair, '--mode', mode, env=env, timeout=180)

        run(setup, '--silent', '--installto', root, env=env, timeout=180)
        probe(root, '1.0.1', env)
        if scenario in ('restore_failure', 'abrupt_after_old', 'consecutive', 'guarded_handoff'):
            arm('1.0.1', '1.0.2')
        if scenario == 'normal':
            apply(root, package, env=env)
            probe(root, '1.0.2', env)
            assert (root / 'tempo-previous' / 'TEMPO.exe').is_file()
            assert (root / 'tempo-update-pending.txt').is_file()
        elif scenario == 'guarded_handoff':
            ready, stop, launched = (work / 'handoff.ready', work / 'handoff.stop', work / 'handoff.launched')
            env.update(TEMPO_FIXTURE_READY=str(ready), TEMPO_FIXTURE_STOP=str(stop),
                       TEMPO_FIXTURE_LAUNCH=str(launched))
            held = subprocess.Popen([str(root / 'current' / 'TEMPO.exe'), '--hold'], env=env)
            companion = None
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and held.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                if not ready.exists():
                    raise RuntimeError('Fixture app failed to acquire its shared lease')
                companion = subprocess.Popen([str(repair), '--mode', 'apply'], env=env,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                # The updater must wait while the live application owns its lease.
                try:
                    output, _ = companion.communicate(timeout=3)
                    raise AssertionError(f'Companion exited while application was active: {output}')
                except subprocess.TimeoutExpired:
                    pass
                if held.poll() is not None or (root / 'current' / 'fixture-version.txt').read_text().strip() != '1.0.1':
                    raise AssertionError('Active application was stopped or replaced')
                stop.write_text('exit', encoding='utf-8')
                if held.wait(timeout=15) != 0:
                    raise RuntimeError('Fixture app did not exit normally')
                output, _ = companion.communicate(timeout=180)
                if companion.returncode:
                    raise RuntimeError(f'Companion apply failed: {output}')
                deadline = time.monotonic() + 30
                while not launched.exists() and time.monotonic() < deadline:
                    time.sleep(.1)
                if not launched.exists() or launched.read_text(encoding='utf-8') != '1.0.2':
                    raise AssertionError('Verified target did not start after companion apply')
                probe(root, '1.0.2', env)
                run(repair, '--mode', 'confirm', '--version', '1.0.2', '--commit', 'b' * 40, env=env)
                if (root / 'tempo-previous').exists() or (root / 'tempo-update-pending.txt').exists():
                    raise AssertionError('Confirmed update still has native pending state')
            finally:
                if held.poll() is None:
                    stop.write_text('exit', encoding='utf-8')
                    try:
                        held.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        held.terminate()
                        held.wait(timeout=10)
                if companion is not None and companion.poll() is None:
                    companion.terminate()
                    companion.communicate(timeout=10)
        elif scenario == 'consecutive':
            apply(root, package, env=env)
            probe(root, '1.0.2', env)  # The frozen application has initialized and answered.
            run(repair, '--mode', 'confirm', '--version', '1.0.2', '--commit', 'b' * 40, env=env)
            assert not (root / 'tempo-previous').exists()
            assert not (root / 'tempo-update-pending.txt').exists()
            arm('1.0.2', '1.0.3')
            apply(root, packages['1.0.3'], env=env)
            probe(root, '1.0.3', env)
            run(repair, '--mode', 'confirm', '--version', '1.0.3', '--commit', 'c' * 40, env=env)
            assert not (root / 'tempo-previous').exists()
            assert not (root / 'tempo-update-pending.txt').exists()
            outcomes['health_confirmation'] = True
            outcomes['two_successive_updates'] = True
        elif scenario == 'uninstall_rename_blocked':
            import ctypes
            from ctypes import wintypes
            import winreg
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            # Permit reads/writes, but hold the root against deletion/rename.
            handle = kernel.CreateFileW(str(root), 0x80, 3, None, 3, 0x02000000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                run(root / 'Update.exe', 'uninstall', '--silent', '--rootDir', root, env=env, expect=1)
                recovery._verify_app(root / 'current', releases['1.0.1'])
                recovery._verify_owner(root, releases['1.0.1'])
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r'Software\Microsoft\Windows\CurrentVersion\Uninstall\TEMPO.RecoveryFixture') as entry:
                    if Path(winreg.QueryValueEx(entry, 'InstallLocation')[0]) != root:
                        raise AssertionError('Failed retirement did not restore uninstall ownership')
            finally:
                kernel.CloseHandle(handle)
            probe(root, '1.0.1', env)
        elif scenario == 'uninstall_reinstall':
            run(root / 'Update.exe', 'uninstall', '--silent', '--rootDir', root, env=env)
            if root.exists():
                raise AssertionError('Uninstall did not retire the reusable installation path')
            retired = list(local.glob('.*.tempo-uninstalled-*'))
            # Reuse immediately, while the retired directory may still be in use.
            run(setup, '--silent', '--installto', root, env=env, timeout=180)
            deadline = time.monotonic() + 30
            while any(p.exists() for p in retired) and time.monotonic() < deadline:
                time.sleep(.1)
            if any(p.exists() for p in retired):
                raise AssertionError('Retired installation cleanup did not complete')
            recovery._verify_app(root / 'current', releases['1.0.1'])
            recovery._verify_owner(root, releases['1.0.1'])
            probe(root, '1.0.1', env)
        elif scenario in ('running_process', 'uninstall_running_process', 'wait_timeout'):
            ready = work / (scenario + '.ready')
            env['TEMPO_FIXTURE_READY'] = str(ready)
            env['TEMPO_FIXTURE_STOP'] = str(work / (scenario + '.stop'))
            held = subprocess.Popen([str(root / 'current' / 'TEMPO.exe'), '--hold'], env=env)
            try:
                deadline = time.monotonic() + 30
                while not ready.is_file() and held.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not ready.is_file():
                    raise RuntimeError('Fixture app failed to signal readiness')
                if scenario == 'uninstall_running_process':
                    result = run(root / 'Update.exe', 'uninstall', '--silent', '--rootDir', root, env=env, expect=None)
                else:
                    result = apply(root, package, env=env,
                                   wait_pid=held.pid if scenario == 'wait_timeout' else None,
                                   inspect_exit=True)
                assert_running_app_refused(result, root, app_alive=held.poll() is None)
            finally:
                Path(env['TEMPO_FIXTURE_STOP']).write_text('exit', encoding='utf-8')
                held.wait(timeout=15)
            probe(root, '1.0.1', env)
        elif scenario == 'hook_cleanup':
            ready, stop, child_pid = (work / 'hook.ready', work / 'hook.stop', work / 'hook.pid')
            env.update(TEMPO_FIXTURE_HOOK_CHILD='1', TEMPO_FIXTURE_READY=str(ready),
                       TEMPO_FIXTURE_STOP=str(stop), TEMPO_FIXTURE_CHILD_PID=str(child_pid))
            try:
                result = apply(root, package, env=env, inspect_exit=True)
                if not ready.exists() or not child_pid.exists():
                    raise AssertionError('Lifecycle hook did not leave its test helper running')
                pid = int(child_pid.read_text(encoding='utf-8'))
                assert_running_app_refused(result, root, app_alive=fixture_process_running(pid))
            finally:
                stop.write_text('exit', encoding='utf-8')
                if child_pid.exists() and fixture_process_running(
                        int(child_pid.read_text(encoding='utf-8')), wait_ms=15000):
                    raise AssertionError('Hook helper did not exit after its normal stop request')
            probe(root, '1.0.1', env)
        elif scenario == 'legacy_writer_guard':
            from tempo_legacy_migration import legacy_writer_guard
            ready, stop = work / 'legacy.ready', work / 'legacy.stop'
            env.update(TEMPO_FIXTURE_READY=str(ready), TEMPO_FIXTURE_STOP=str(stop))
            expected = next(item.sha256 for item in releases['1.0.1'].inventory
                            if item.path == 'lib/app/TEMPO.exe')
            held = subprocess.Popen([str(root / 'current' / 'TEMPO.exe'), '--hold'], env=env)
            try:
                deadline = time.monotonic() + 30
                while not ready.exists() and held.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                if not ready.exists():
                    raise AssertionError('Legacy writer fixture did not start')
                try:
                    with legacy_writer_guard(root / 'current', expected):
                        raise AssertionError('Migration accepted a running legacy writer')
                except BlockingIOError:
                    pass
                if held.poll() is not None:
                    raise AssertionError('Migration guard terminated the old application')
            finally:
                stop.write_text('exit', encoding='utf-8')
                held.wait(timeout=15)
            ready.unlink(); stop.unlink()
            with legacy_writer_guard(root / 'current', expected):
                try:
                    child = subprocess.Popen([str(root / 'current' / 'TEMPO.exe'), '--hold'], env=env)
                except OSError:
                    pass  # Windows must deny executable reads while preparation owns the guard.
                else:
                    stop.write_text('exit', encoding='utf-8')
                    child.wait(timeout=15)
                    raise AssertionError('Old application started during migration preparation')
            probe(root, '1.0.1', env)
        elif scenario == 'before_old_rename':
            apply(root, package, env=env, fault='before_old_rename')
            probe(root, '1.0.1', env)
        elif scenario == 'after_old_rename':
            apply(root, package, env=env, fault='after_old_rename')
            probe(root, '1.0.1', env)
            assert (root / 'tempo-update-pending.txt').is_file()
        else:
            if scenario == 'restore_failure':
                apply(root, package, env=env, fault='after_old_rename,before_restore')
            else:
                apply(root, package, env=env, abort='after_old_rename')
            assert not (root / 'current').exists()
            assert (root / 'tempo-previous' / 'TEMPO.exe').is_file()
            assert (root / 'tempo-update-pending.txt').is_file()
            # Neither a working native updater nor its marker is available to repair.
            if scenario == 'restore_failure':
                (root / 'Update.exe').write_bytes(b'broken fixture updater')
            else:
                (root / 'Update.exe').unlink()
            (root / 'tempo-update-pending.txt').write_text('damaged marker', encoding='utf-8')
            mode = 'previous' if scenario == 'restore_failure' else 'forward'
            repair_mode(mode)
            recovery._verify_owner(root, releases['1.0.1' if mode == 'previous' else '1.0.2'])
            probe(root, '1.0.1' if mode == 'previous' else '1.0.2', env)
            outcomes['offline_' + mode] = True
            if mode == 'forward':
                run(repair, '--mode', 'confirm', '--version', '1.0.2', '--commit', 'b' * 40, env=env)
                arm('1.0.2', '1.0.3')
                apply(root, packages['1.0.3'], env=env)
                probe(root, '1.0.3', env)
                outcomes['repaired_owner_next_update'] = True
        # This root and package ID are fixture-only. Uninstall uses the real updater.
        run(root / 'Update.exe', 'uninstall', '--silent', '--rootDir', root, env=env)
        outcomes[scenario] = True
        report()
    outcomes['independent_repair'] = outcomes['restore_failure'] and outcomes['abrupt_after_old']
    report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vpk-root', type=Path, required=True)
    parser.add_argument('--fixture-updater', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        raise RuntimeError('The native fixture requires Windows')
    outcomes = {}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    def report(error=None):
        args.report.write_text(json.dumps({'scenarios': outcomes, 'certified': False,
                                           'error': error}, indent=2), encoding='utf-8')
    report()
    try:
        with tempfile.TemporaryDirectory(prefix='tempo-velopack-fixture-') as tmp:
            work = Path(tmp)
            try:
                setup, packages = build_fixture(work, args.vpk_root.resolve(), args.fixture_updater.resolve())
                key = Ed25519PrivateKey.generate()
                raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                keys = {'fixture': raw}
                config = work / 'public-keys.json'
                config.write_text(json.dumps({'fixture': base64.b64encode(raw).decode()}), encoding='utf-8')
                run(sys.executable, PROJECT / 'scripts' / 'build_update_repair.py', '--public-keys', config,
                    '--native-updater', args.fixture_updater.resolve(), '--dist', work / 'repair-dist', '--work', work / 'repair-build', timeout=300)
                repair = work / 'repair-dist' / 'TEMPORepair.exe'
                releases = {v: signed_release(p, v, key, keys) for v, p in packages.items()}
                run_scenarios(work, setup, packages, repair, releases, keys, outcomes, report)
            finally:
                logs = list(work.glob('profile-*/TEMPO/updates/recovery/native-apply.log'))
                if logs:
                    evidence = args.report.parent / 'fixture-native-logs'
                    evidence.mkdir(parents=True, exist_ok=True)
                    for log in logs:
                        shutil.copyfile(log, evidence / (log.relative_to(work).parts[0] + '.log'))
    except Exception:
        report(traceback.format_exc())
        raise
    print('Fixture scenarios passed; production certification still requires separate evidence.')


if __name__ == '__main__':
    main()
