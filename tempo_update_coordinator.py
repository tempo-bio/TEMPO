"""Packaged application update orchestration; the GUI never replaces files."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys

from tempo_update_manifest import TransferPlan
from tempo_update_service import UpdateService, read_configuration


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
        self.nvidia_receipt = None

    def check(self, *, cancel=None):
        releases = self.service.check(cancel=cancel)
        self.base_release = next((r for r in releases if
                                  (r.version, r.commit) == (self.version, self.commit)), None)
        self.base_package = None
        if self.base_release:
            candidates = (self.service.root / 'packages' / self.base_release.full.name,
                          self.installation.root / 'packages' / self.base_release.full.name)
            self.base_package = next((p for p in candidates if p.is_file()), None)
        try:
            self.plan = self.service.plan(installed_version=self.version,
                installed_commit=self.commit, base_release=self.base_release,
                base_package=self.base_package)
        except ValueError as exc:
            # A current version is an ordinary check result. Other errors stay visible.
            if str(exc) != 'Automatic downgrade or reinstall is not permitted.':
                raise
            self.plan = None
        return self.plan

    def use_full_download(self):
        """Issue a fresh authenticated full plan after explicit fallback acceptance."""
        self.plan = self.service.plan(installed_version=self.version,
            installed_commit=self.commit, base_release=None, base_package=None)
        return self.plan

    def download(self, *, cancel=None, progress=lambda received, total: None):
        if self.plan is None:
            raise ValueError('Check for an available update first.')
        configuration = self.configuration
        return self.service.prepare(self.plan, base_release=self.base_release,
            base_package=self.base_package, patcher=self.installation.uninstaller,
            patcher_sha256=configuration.get('updater_sha256'), cancel=cancel,
            progress=progress)

    def prepare_components(self, *, progress=lambda received, total: None):
        """Stage a target worker without selecting it for the running application."""
        from tempo_nvidia_runtime import active_executable
        if active_executable() is None:
            return None
        release, _ = self.service.verified_prepared()
        encoded = getattr(release, 'nvidia_descriptor', None)
        if encoded is None:
            raise ValueError('This update does not provide a matching NVIDIA worker. Defer it or explicitly choose CPU support.')
        from tempo_nvidia_setup import stage_nvidia_runtime
        self.nvidia_receipt = stage_nvidia_runtime(json.loads(encoded),
            target_version=release.version, target_commit=release.commit, report=progress)
        from tempo_annotation_store import atomic_bytes
        atomic_bytes(self.service.root / 'prepared-nvidia.json',
                     json.dumps(self.nvidia_receipt, allow_nan=False).encode('utf-8'))
        return self.nvidia_receipt

    def installation_consequences(self):
        """Return compatibility blockers before requesting application shutdown."""
        from tempo_update_migration import discover_addons
        addons = discover_addons(self.installation.root / 'current')
        addons += discover_addons(self.installation.root)
        if addons:
            raise ValueError('An optional package modifies this installation. Remove it using its verified removal procedure or defer the update.')
        if self.base_release is None or self.base_package is None:
            raise ValueError('A verified offline recovery package is required before installation. Repair this installation before updating.')
        return self.service.verified_prepared()

