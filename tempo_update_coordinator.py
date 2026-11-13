"""Packaged application update orchestration; the GUI never replaces files."""
from __future__ import annotations

import base64
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from zipfile import BadZipFile
from zlib import error as DecompressionError

from tempo_update_manifest import verify_zip
from tempo_update_service import UpdateService, read_configuration, _safe_directory


def configured_coordinator():
    """Unsupported and uncertified installations retain their manual update path."""
    from tempo_update_installation import detect_installation
    installation = detect_installation()
    if installation.kind != 'velopack':
        return None
    from tempo_config import get_base_dir, get_config_dir
    configuration = read_configuration(get_base_dir())
    if configuration is None:
        return None
    from tempo_release import VERSION, build_identity
    return PackagedUpdateCoordinator(installation, configuration,
        Path(get_config_dir()).parent, VERSION, build_identity()['commit'])


class PackagedUpdateCoordinator:
    def __init__(self, installation, configuration, profile_root, version, commit, *, service=None):
        self.installation = installation
        self.configuration = configuration
        self.profile = Path(profile_root)
        self.version, self.commit = version, commit
        keys = {name: base64.b64decode(value, validate=True)
                for name, value in configuration['trusted_keys'].items()}
        self.service = service or UpdateService(self.profile / 'updates', keys,
                                               channel=configuration['channel'])
        self.plan = None
        self.base_release = None
        self.base_package = None
        self.recovery_download_size = 0
        self.nvidia_receipt = None
        self.component_required = False
        self.component_download_size = 0
        self.repair_executable = None
        self._handoff_error = None

    def check(self, *, cancel=None):
        self.nvidia_receipt = None
        self.recovery_download_size = 0
        releases = self.service.check(cancel=cancel)
        self.base_release = next((r for r in releases if
                                  (r.version, r.commit) == (self.version, self.commit)), None)
        self.base_package = None
        if self.base_release:
            candidates = (self.service.recovery_package_path(self.base_release),
                          self.service.root / 'packages' / self.base_release.full.name,
                          self.installation.root / 'packages' / self.base_release.full.name,
                          self.service.root / 'recovery' / 'target.nupkg',
                          self.service.root / 'recovery' / 'base.nupkg',
                          self.service.root / 'legacy-migration' / 'target.nupkg')
            for path in candidates:
                try:
                    verify_zip(_safe_directory(path), self.base_release.inventory)
                except (OSError, ValueError, BadZipFile, EOFError, RuntimeError, DecompressionError):
                    continue
                self.base_package = path
                break
        try:
            self.plan = self.service.plan(installed_version=self.version,
                installed_commit=self.commit, base_release=self.base_release,
                base_package=self.base_package)
        except ValueError as exc:
            # A current version is an ordinary check result. Other errors stay visible.
            if str(exc) != 'Automatic downgrade or reinstall is not permitted.':
                raise
            self.plan = None
        if self.plan is not None:
            if self.base_release is None:
                raise ValueError('The update feed is missing the installed version. Use the verified installer or repair instructions.')
            if self.base_package is None:
                self.recovery_download_size = self.base_release.full.size
        from tempo_update_components import read_runtime_marker
        self.component_required = self.plan is not None and read_runtime_marker(self.profile / 'nvidia-runtime') is not None
        self.component_download_size = 0
        if self.component_required:
            encoded = self.plan.target.nvidia_descriptor
            if encoded is None:
                raise ValueError('This update has no matching NVIDIA support. Defer it until a compatible release is available.')
            self.component_download_size = json.loads(encoded)['archive_size']
        return self.plan

    def use_full_download(self):
        """Issue a fresh authenticated full plan after explicit fallback acceptance."""
        self.plan = self.service.plan(installed_version=self.version,
            installed_commit=self.commit, base_release=self.base_release,
            base_package=self.base_package, force_full=True)
        return self.plan

    def download(self, *, cancel=None, progress=lambda received, total: None,
                 report_stage=lambda stage: None):
        if self.plan is None:
            raise ValueError('Check for an available update first.')
        configuration = self.configuration
        return self.service.prepare(self.plan,
            base_release=self.base_release, base_package=self.base_package,
            patcher=self.installation.uninstaller,
            patcher_sha256=configuration.get('updater_sha256'), cancel=cancel,
            progress=progress, report_stage=report_stage)

    def prepare_components(self, *, progress=lambda received, total: None,
                           report_stage=lambda stage: None):
        """Stage a target worker without selecting it for the running application."""
        if not self.component_required:
            return None
        release, package = self.service.verified_prepared()
        from tempo_update_components import packaged_descriptor
        descriptor = packaged_descriptor(release, package)
        from tempo_nvidia_setup import stage_nvidia_runtime
        self.nvidia_receipt = stage_nvidia_runtime(descriptor,
            target_version=release.version, target_commit=release.commit,
            report=progress, report_stage=report_stage)
        from tempo_annotation_store import atomic_bytes
        atomic_bytes(self.service.root / 'prepared-nvidia.json',
                     json.dumps(self.nvidia_receipt, allow_nan=False).encode('utf-8'))
        return self.nvidia_receipt

    def confirm_startup(self):
        """Acknowledge a rendered desktop, after selecting its verified worker."""
        record_path = self.service.root / 'recovery' / 'recovery.json'
        if not record_path.exists():
            return False
        from tempo_update_recovery import _read_record, _load_pair
        _, record, root, external, _ = _read_record(record_path,
            self.installation.root, self.service.root / 'packages')
        if record['state'] == 'confirmed':
            return False
        (_, _), (target, package) = _load_pair(external, record, self.service.keys)
        if (self.version, self.commit) != (target.version, target.commit):
            raise ValueError('The running application differs from the pending update. Use explicit repair before updating again.')
        if record['nvidia_marker'] is not None:
            path = _safe_directory(self.service.root / 'prepared-nvidia.json')
            if path.stat().st_size > 65536:
                raise ValueError('Invalid staged NVIDIA receipt.')
            receipt = json.loads(path.read_bytes())
            if receipt.get('previous_marker') != record['nvidia_marker'] or target.nvidia_descriptor is None:
                raise ValueError('NVIDIA selection differs from the pending update.')
            from tempo_update_components import activate_staged_runtime, packaged_descriptor
            activate_staged_runtime(receipt, packaged_descriptor(target, package),
                runtime_root=self.profile / 'nvidia-runtime', previous_marker=record['nvidia_marker'])
            from tempo_nvidia_runtime import active_executable
            if active_executable() is None:
                raise ValueError('The matching NVIDIA worker could not be selected. Recovery confirmation is deferred.')
        self._verify_native_tools()
        result = subprocess.run([str(self.repair_executable), '--mode', 'confirm',
            '--version', self.version, '--commit', self.commit], check=False,
            timeout=120, capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('The update is running, but recovery confirmation failed. Retained repair material is still available. ' +
                               result.stderr.decode('utf-8', errors='replace')[-2000:])
        return True

    def installation_consequences(self):
        """Return compatibility blockers before requesting application shutdown."""
        from tempo_update_migration import discover_addons
        addons = discover_addons(self.installation.root / 'current')
        addons += discover_addons(self.installation.root)
        if addons:
            raise ValueError('An optional package modifies this installation. Remove it using its verified removal procedure or defer the update.')
        base_package = self.base_package
        if self.recovery_download_size:
            base_package = self.service.recovery_package_path(self.base_release)
        if self.base_release is None or base_package is None:
            raise ValueError('A verified offline recovery package is required before installation. Repair this installation before updating.')
        target, package = self.service.verified_prepared()
        from tempo_update_components import read_runtime_marker, verify_component_transition
        marker = read_runtime_marker(self.profile / 'nvidia-runtime')
        if marker is not None and self.nvidia_receipt is None:
            raise ValueError('Prepare matching NVIDIA support before installing this update.')
        verify_component_transition(self.base_release, target, package, self.nvidia_receipt,
            runtime_root=self.profile / 'nvidia-runtime', expected_marker=marker)
        # Companions use the reserved per-user identity, never a record-supplied root.
        expected_root = Path(os.environ['LOCALAPPDATA']) / 'TEMPO.Bio.Desktop'
        if self.installation.root != expected_root:
            raise ValueError('Automatic updates require the supported per-user installation location.')
        self._verify_native_tools()
        from tempo_update_recovery import arm_recovery
        self.recovery_record = arm_recovery(self.installation.root,
            self.service.root / 'recovery', self.base_release, base_package,
            target, package, trusted_keys=self.service.keys,
            packages_dir=self.service.root / 'packages', expected_root=expected_root,
            nvidia_marker=marker)
        return target, package

    def _verify_native_tools(self):
        from tempo_update_recovery import _store_copy
        updater = _safe_directory(self.installation.uninstaller)
        repair = _safe_directory(self.installation.root / 'current' / 'TEMPORepair.exe')
        for path, field in ((updater, 'updater_sha256'), (repair, 'repair_sha256')):
            if hashlib.sha256(path.read_bytes()).hexdigest() != self.configuration.get(field):
                raise ValueError('The installed update or repair tool failed certification verification.')
        tools = _safe_directory(self.service.root / 'tools')
        tools.mkdir(parents=True, exist_ok=True)
        self.repair_executable = tools / ('TEMPORepair-' + self.configuration['repair_sha256'] + '.exe')
        _store_copy(repair, self.repair_executable)

    def request_install(self, parent):
        """Queue a handoff through the desktop's existing save/hardware guards."""
        if parent is None or not hasattr(parent, 'request_close_action'):
            raise ValueError('Open this update from the TEMPO desktop before installing.')
        if not getattr(self, 'recovery_record', None) or self.repair_executable is None:
            raise ValueError('Prepare verified offline recovery before requesting restart.')
        if getattr(parent, '_close_action', None) is not None:
            raise RuntimeError('Another operation is already waiting for TEMPO to close.')

        def handoff():
            try:
                self._verify_native_tools()
                # The independent companion waits for the desktop/worker lease;
                # it reauthenticates the journal and payloads after actual exit.
                subprocess.Popen([str(self.repair_executable), '--mode', 'apply', '--show-errors'],
                    cwd=str(self.service.root), close_fds=True,
                    creationflags=(getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) |
                                   getattr(subprocess, 'CREATE_NO_WINDOW', 0)))
                return True
            except Exception as exc:
                self._handoff_error = exc
                parent.statusBar().showMessage('Could not start the update. The current application is retained. ' + str(exc))
                return False
        parent.request_close_action(handoff)
        # Qt close() returns False while asynchronous guards are pending.
        return self._handoff_error is None and (
            getattr(parent, '_close_action', None) is handoff or not parent.isVisible())

    def uninstall_command(self):
        """Prepare an independent owner that waits for actual application exit."""
        if self.installation.root != Path(os.environ['LOCALAPPDATA']) / 'TEMPO.Bio.Desktop':
            raise ValueError('The installation is outside the supported per-user location.')
        self._verify_native_tools()
        return [str(self.repair_executable), '--mode', 'uninstall', '--show-errors']

