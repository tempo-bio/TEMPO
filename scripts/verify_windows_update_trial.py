"""Exercise signed transfers, Windows replacement and offline repair using private builds."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError

from scripts.build_update_repair import read_keys
from scripts.build_windows_update_trial import NATIVE_SHA256
from scripts.package_windows_updates import _digest
from scripts.prepare_windows_update_trial import VERSIONS
from scripts.windows_release_checks import isolated_environment
from tempo_release import REQUIRED_CHECKS
from tempo_update_manifest import PACKAGE_ID, validate_path, verify_asset
from tempo_update_recovery import _verify_app, _verify_owner, arm_recovery
from tempo_update_service import FEEDS, FullDownloadRequired, UpdateService


class LocalTrialFeed:
    """Supply the exact signed assets on disk and inject one failed delta download."""

    def __init__(self, directory, index, *, fail_asset=None):
        self.directory = directory
        self.index = index
        self.fail_asset = fail_asset
        self.requested = []

    def __call__(self, request, timeout):
        url = request.full_url
        name = self.index + '.json' if url == FEEDS['prerelease'] else url.rsplit('/', 1)[-1]
        if '/' in validate_path(name):
            raise ValueError('Trial assets must be simple filenames.')
        self.requested.append(name)
        if name == self.fail_asset:
            raise HTTPError(url, 503, 'Injected failure for the trial delta', None, None)
        response = (self.directory / name).open('rb')
        response.geturl = lambda: url
        return response


def prepare_update(service, version, target, base_package, patcher, kind, *, failure=None):
    releases = service.check()
    base = next(release for release in releases if release.version == version)
    options = dict(installed_version=base.version, installed_commit=base.commit,
                   base_release=base, base_package=base_package)
    plan = service.plan(**options)
    if plan.kind != kind or plan.target.version != target:
        raise ValueError(f'Expected the {kind} plan to {target}; received {plan.kind} to {plan.target.version}.')
    try:
        service.prepare(plan, base_release=base, base_package=base_package, patcher=patcher,
                        patcher_sha256=NATIVE_SHA256['update.exe'])
    except FullDownloadRequired as error:
        cause = error.__cause__
        if failure == 'transfer':
            if not isinstance(cause, HTTPError) or cause.code != 503:
                raise
        elif failure == 'reconstruction':
            if str(cause) != 'Update reconstruction failed.':
                raise
        else:
            raise
        if (service.root / 'prepared.json').exists():
            raise ValueError('A failed delta published a ready package.')
        full = service.plan(**options, force_full=True)
        if full.kind != 'full' or full.target != plan.target:
            raise ValueError('Fallback did not keep the authenticated target.')
        service.prepare(full, base_release=base, base_package=base_package)
    else:
        if failure:
            raise ValueError(f'The expected {failure} failure did not occur.')
    release, package = service.verified_prepared()
    if release != plan.target:
        raise ValueError('The prepared package changed its authenticated identity.')
    if base_package is None:
        verify_asset(service.recovery_package_path(base), base.full)
    return package, base, release


def run_windows(command, env, *, timeout=600):
    with tempfile.TemporaryFile() as log:
        result = subprocess.run([str(item) for item in command], env=env, timeout=timeout,
                                stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 4000))
            detail = log.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'{Path(command[0]).name} exited with {result.returncode}: {detail}')


@contextmanager
def trial_directory(parent, prefix):
    # TemporaryDirectory resets read-only Windows shell files and directories.
    temporary = tempfile.TemporaryDirectory(dir=parent, prefix=prefix)
    try:
        yield Path(temporary.name)
    finally:
        deadline = time.monotonic() + 60
        while True:
            try:
                temporary.cleanup()
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
            else:
                break


@contextmanager
def installed_trial(trial, contract, directory):
    """Install into a fresh profile on the disposable CI runner."""
    import winreg

    registry = rf'Software\Microsoft\Windows\CurrentVersion\Uninstall\{PACKAGE_ID}'
    try:
        existing = winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry)
    except FileNotFoundError:
        pass
    else:
        existing.Close()
        raise ValueError('The trial requires an unused Windows installation registration.')
    env = isolated_environment(directory)
    root = Path(env['LOCALAPPDATA']) / PACKAGE_ID
    profile = root.parent / 'TEMPO'
    profile.mkdir()
    state = profile / 'trial-recording.txt'
    state.write_bytes(b'Test recording outside the installation')
    pwsh = shutil.which('pwsh')
    if pwsh is None:
        raise ValueError('PowerShell is required to prepare the isolated Windows shell folders.')
    run_windows([pwsh, '-NoProfile', '-NonInteractive', '-Command',
                 "[Environment]::GetFolderPath('DesktopDirectory', 'Create'); "
                 "[Environment]::GetFolderPath('Programs', 'Create')"], env, timeout=30)
    setup = trial / 'trial-setup.exe'
    repair = trial / 'TEMPORepair.exe'
    for path, role in ((setup, 'installer'), (repair, 'repair')):
        if _digest(path) != contract[role]['sha256']:
            raise ValueError(f'Trial {role} checksum mismatch.')
    run_windows([setup, '--silent', '--installto', root], env)
    yield root, profile, env
    run_windows([repair, '--mode', 'uninstall'], env)
    if root.exists() or state.read_bytes() != b'Test recording outside the installation':
        raise ValueError('Uninstall did not retire the trial installation or preserve the sample recording.')
    # The pinned updater moves the old installation aside, then cleans it up
    # in a detached process. Keep its parent available until that finishes.
    retired = f'.{PACKAGE_ID}.tempo-uninstalled-*'
    deadline = time.monotonic() + 60
    while any(root.parent.glob(retired)):
        if time.monotonic() >= deadline:
            raise TimeoutError('The detached uninstaller did not finish retiring the trial installation.')
        time.sleep(.2)


def require_startup(report, release):
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            value = json.loads(report.read_bytes())
        except (OSError, ValueError):
            pass  # The frozen diagnostics rewrite the report while they run.
        else:
            if value.get('success') is True:
                if (value.get('frozen') is not True or value.get('version') != release.version
                        or value.get('commit') != release.commit
                        or set(value.get('checks', ())) != set(REQUIRED_CHECKS)):
                    raise ValueError('The restarted application reported a different release or diagnostics contract.')
                return
            if 'active_check' not in value and value.get('error'):
                raise RuntimeError(f'The restarted application failed diagnostics: {value["error"]}')
        time.sleep(1)
    raise TimeoutError(f'The restarted application did not pass diagnostics: {report.name}')


def replace_or_repair(trial, installed, base, base_package, target, package, keys, report, *, offline=False):
    root, profile, env = installed
    _verify_app(root / 'current', base)
    _verify_owner(root, base)
    recovery = profile / 'updates' / 'recovery'
    record = arm_recovery(root, recovery, base, base_package, target, package, trusted_keys=keys,
                          packages_dir=profile / 'updates' / 'packages', expected_root=root)
    repair = trial / 'TEMPORepair.exe'
    report.unlink(missing_ok=True)
    if offline:
        # Damage only the isolated fixture after both signed recovery packages are ready.
        (root / 'current').rename(profile / 'interrupted-current')
        (root / 'Update.exe').rename(profile / 'interrupted-owner.exe')
        run_windows([repair, '--mode', 'forward'], env)
        run_windows([root / 'current' / 'TEMPO.exe', '--verify-installation', report], env, timeout=900)
    else:
        try:
            run_windows([repair, '--mode', 'apply'], dict(env, TEMPO_SIGNED_TRIAL_REPORT=str(report)))
        finally:
            log = recovery / 'native-apply.log'
            if log.is_file():
                shutil.copyfile(log, report.with_suffix('.log'))
    require_startup(report, target)
    run_windows([repair, '--mode', 'confirm', '--version', target.version, '--commit', target.commit], env)
    if json.loads(record.read_bytes())['state'] != 'confirmed':
        raise ValueError('The native update did not confirm its signed target.')
    _verify_app(root / 'current', target)
    _verify_owner(root, target)


def verify(trial, patcher, report_path):
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('Run the signed trial on a fresh GitHub Actions Windows runner.')
    trial, patcher, report_path = (Path(item).resolve() for item in (trial, patcher, report_path))
    if _digest(patcher) != NATIVE_SHA256['update.exe']:
        raise ValueError('The Windows patch engine differs from the reviewed v1.0.2 binary.')
    keys = read_keys(trial / 'trial-public-keys.json')
    contract = json.loads((trial / 'trial-contract.json').read_bytes())
    if (set(keys) != {'acceptance-only'} or contract.get('purpose') != 'isolated-signed-update-trial'
            or contract.get('release_certified') is not False):
        raise ValueError('Use the isolated trial packages and their public test key.')
    a, b, c = VERSIONS
    initial = trial / contract['revisions'][0]['full']['path']
    first_index = json.loads((trial / 'first-update.json').read_bytes())
    delta, = first_index['releases'][1]['signed']['patches']
    outcomes = {}
    report = dict(success=False, release_certified=False, scenarios=outcomes,
                  trial_contract_sha256=_digest(trial / 'trial-contract.json'),
                  patcher_sha256=NATIVE_SHA256['update.exe'])
    report_path.parent.mkdir(parents=True, exist_ok=True)

    def exercise(index, version, target, base_package, kind, *, failure=None, fail_asset=None, root):
        feed = LocalTrialFeed(trial, index, fail_asset=fail_asset)
        service = UpdateService(root, keys, channel='prerelease', opener=feed)
        package, base, release = prepare_update(service, version, target, base_package, patcher, kind, failure=failure)
        outcomes[root.name] = dict(base=base.version, target=release.version, plan=kind, fallback=failure,
                                   prepared_sha256=_digest(package), requested=feed.requested)
        return package, base, release

    try:
        with trial_directory(trial.parent, '.tempo-trial-verify-') as root:
            with trial_directory(root, 'sequence-') as sequence:
                with installed_trial(trial, contract, sequence / 'installation') as installed:
                    first, base, target = exercise('first-update', a, b, initial, 'patch', root=sequence / 'first')
                    replace_or_repair(trial, installed, base, initial, target, first, keys,
                                      report_path.with_name('trial-first-startup.json'))
                    outcomes['first']['native_replacement_confirmed'] = True
                    second, base, target = exercise('next-update', b, c, first, 'patch', root=sequence / 'next')
                    replace_or_repair(trial, installed, base, first, target, second, keys,
                                      report_path.with_name('trial-next-startup.json'))
                    outcomes['next']['native_replacement_confirmed'] = True
            for name, index, target, cached, kind, failure, failed_asset in (
                ('skip', 'skip-version', c, initial, 'full', None, None),
                ('transfer-fallback', 'first-update', b, initial, 'patch', 'transfer', delta['asset']['name']),
                ('reconstruction-fallback', 'invalid-reconstruction', b, initial, 'patch', 'reconstruction', None),
                ('download-recovery-base', 'first-update', b, None, 'full', None, None),
            ):
                with trial_directory(root, 'case-') as scenario:
                    package, base, release = exercise(index, a, target, cached, kind, failure=failure,
                                                       fail_asset=failed_asset, root=scenario / name)
                    if name in ('skip', 'reconstruction-fallback'):
                        with installed_trial(trial, contract, scenario / 'installation') as installed:
                            offline = name == 'reconstruction-fallback'
                            replace_or_repair(trial, installed, base, cached, release, package, keys,
                                              report_path.with_name(f'trial-{name}-startup.json'), offline=offline)
                            outcomes[name]['offline_repair_confirmed' if offline else 'native_replacement_confirmed'] = True
                            if offline:
                                following, base, target = exercise('next-update', b, c, package, 'patch',
                                                                    root=scenario / 'after-repair')
                                replace_or_repair(trial, installed, base, package, target, following, keys,
                                                  report_path.with_name('trial-after-repair-startup.json'))
                                outcomes['after-repair']['native_replacement_confirmed'] = True
        report['success'] = True
    except Exception as error:
        report['error'] = str(error)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trial', type=Path, required=True)
    parser.add_argument('--patcher', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    verify(args.trial, args.patcher, args.report)


if __name__ == '__main__':
    main()
