"""Test the stable index from a versioned prerelease without switching the live feed."""

import argparse
import base64
import hashlib
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import shutil
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

from scripts.build_velopack_candidate import file_record
from scripts.verify_release_delta import BASES
from scripts.verify_windows_update_trial import installed_trial, run_windows, trial_directory
from scripts.windows_release_checks import validate_report
from tempo_update_recovery import _verify_app, _verify_owner, arm_recovery
from tempo_update_service import read_configuration


def wait_for_confirmation(record):
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        state = json.loads(record.read_bytes())['state']
        if state == 'confirmed':
            return
        if state == 'recovery_required':
            raise ValueError('The staged update requires repair.')
        time.sleep(1)
    raise TimeoutError('The updated desktop did not confirm startup.')


def stop_fixture_desktop(executable, env):
    # The legacy companion reopens the desktop automatically. Retire only
    # the process running the exact application in this disposable profile.
    run_windows(['pwsh', '-NoProfile', '-NonInteractive', '-Command',
        "$ErrorActionPreference='Stop'; Get-Process -Name TEMPO -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -eq $env:TEMPO_TEST_DESKTOP } | "
        "ForEach-Object { Stop-Process -InputObject $_ -Force; $_.WaitForExit() }"],
        dict(env, TEMPO_TEST_DESKTOP=str(executable)), timeout=30)


def verify(files, evidence, old_service, commit, index_hash, target_version, route):
    if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('Use a disposable Windows GitHub Actions runner.')
    files, evidence = files.resolve(), evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    index = files / 'tempo-update-index.json'
    if hashlib.sha256(index.read_bytes()).hexdigest() != index_hash:
        raise ValueError('The versioned prerelease serves a different index.')
    base_version, installer_hash = BASES[target_version]
    installer = files / f'TEMPO_Setup_v{base_version}.exe'
    installer_record = file_record(installer)
    if installer_record['sha256'] != installer_hash:
        raise ValueError(f'Use the exact public v{base_version} installer.')
    # Load just the released transfer service. It will use its own default
    # transport and validation, with a redirect only for the index request.
    spec = spec_from_file_location('release_base_update_service', old_service)
    client = module_from_spec(spec)
    spec.loader.exec_module(client)
    staged_url = f'https://github.com/tempo-bio/TEMPO/releases/download/v{target_version}/tempo-update-index.json'
    failed_delta = None
    requests = []

    def response(request, timeout):
        url = request.full_url
        if url == failed_delta:
            raise HTTPError(url, 503, 'Injected unavailable delta', None, None)
        requests.append(url)
        if url == client.FEEDS['stable']:
            # Use the exact index bytes already retrieved and checksum-verified.
            stream = index.open('rb')
            stream.geturl = lambda: staged_url
            return stream
        return urlopen(request, timeout=timeout)

    new_repair = files / 'TEMPORepair.exe'
    with trial_directory(evidence.parent, 'staged-update-') as work:
        (work / 'trial-setup.exe').hardlink_to(installer)
        (work / 'TEMPORepair.exe').hardlink_to(new_repair)
        trial = {'installer': installer_record, 'repair': file_record(new_repair)}
        with installed_trial(work, trial, work / 'profile') as (root, profile, env):
            bundle = root / 'current' / '_internal'
            config = read_configuration(bundle)
            if not config or config['channel'] != 'stable':
                raise ValueError('The public base does not have the expected stable trust configuration.')
            keys = {name: base64.b64decode(value, validate=True) for name, value in config['trusted_keys'].items()}
            service = client.UpdateService(profile / 'updates', keys, channel='stable', opener=response)
            releases = service.check()
            base = next(item for item in releases if item.version == base_version)
            _verify_app(root / 'current', base)
            owner = _verify_owner(root, base)
            base_full = root / 'packages' / base.full.name
            if not base_full.is_file():
                # Customers can also start with a recovery download if their
                # installer cache has been removed. The full route exercises it.
                base_full = files / base.full.name
            options = dict(installed_version=base.version, installed_commit=base.commit,
                           base_release=base, base_package=base_full)
            plan = service.plan(**options)
            target = plan.target
            if (target.version != target_version or target.commit != commit or plan.kind != 'patch'
                    or len(plan.assets) != 1):
                raise ValueError('The installed client did not select the intended signed delta.')
            repair_hash = next(item.sha256 for item in target.inventory if item.path == 'lib/app/TEMPORepair.exe')
            if repair_hash != trial['repair']['sha256']:
                raise ValueError('The served repair companion differs from the signed inventory.')
            if route == 'full-fallback':
                failed_delta = plan.assets[0].url
            try:
                service.prepare(plan, base_release=base, base_package=base_full, patcher=owner,
                                patcher_sha256=config['updater_sha256'])
            except client.FullDownloadRequired as error:
                if (route != 'full-fallback' or not isinstance(error.__cause__, HTTPError)
                        or error.__cause__.url != failed_delta):
                    raise
                options['base_package'] = None
                plan = service.plan(**options, force_full=True)
                service.prepare(plan, base_release=base)
                base_full = service.recovery_package_path(base)
            else:
                if route != 'delta':
                    raise ValueError('The client did not exercise the requested full fallback.')
            selected, package = service.verified_prepared()
            if selected != target:
                raise ValueError('Preparation changed the authenticated target.')
            record = arm_recovery(root, service.root / 'recovery', base, base_full, target, package,
                                  trusted_keys=keys, packages_dir=service.root / 'packages', expected_root=root)
            repair = work / 'installed-base-repair.exe'
            shutil.copyfile(root / 'current' / 'TEMPORepair.exe', repair)
            state = profile / 'config' / 'setup_state.json'
            state.parent.mkdir(exist_ok=True)
            state.write_text('{"schema_version":1,"stage":"complete"}', encoding='utf-8')
            executable = root / 'current' / 'TEMPO.exe'
            try:
                run_windows([repair, '--mode', 'apply'], env)
                wait_for_confirmation(record)
                _verify_app(root / 'current', target)
                _verify_owner(root, target)
            finally:
                stop_fixture_desktop(executable, env)
                log = record.parent / 'native-apply.log'
                if log.is_file():
                    shutil.copyfile(log, evidence / 'native-apply.log')
            report = evidence / 'target-runtime.json'
            run_windows([executable, '--verify-installation', report], env, timeout=900)
            validate_report(json.loads(report.read_bytes()), commit=commit)
            if state.read_text(encoding='utf-8') != '{"schema_version":1,"stage":"complete"}':
                raise ValueError('The update changed the onboarding state.')
    (evidence / 'staged-update.json').write_text(json.dumps({
        'success': True, 'route': route, 'base_installer': installer_record,
        'target_commit': commit, 'target_package_sha256': target.full.sha256,
        'index_sha256': index_hash, 'requests': requests, 'desktop_confirmed': True,
    }, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('files', 'evidence', 'old-service'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--index-hash', required=True)
    parser.add_argument('--target-version', required=True, choices=('1.0.5', '1.0.6'))
    parser.add_argument('--route', choices=('delta', 'full-fallback'), required=True)
    verify(**vars(parser.parse_args()))
