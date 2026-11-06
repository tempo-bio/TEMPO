"""Read-only identification and early startup for Windows installations."""
from dataclasses import dataclass
from pathlib import Path
import sys
import atexit
import xml.etree.ElementTree as ET

PACKAGE_ID = 'TEMPO.Bio.Desktop'
_LIFECYCLE = frozenset(('--veloapp-install', '--veloapp-updated',
                        '--veloapp-obsolete', '--veloapp-uninstall'))
_WORKERS = frozenset(('--assist-model', '--assist-model-cuda', '--probe-cuda',
                      '--predict-cells', '--predict-nuclei', '--verify-installation',
                      '--verify-probe'))
_application_lease = None


@dataclass(frozen=True)
class Installation:
    kind: str
    root: Path | None = None
    uninstaller: Path | None = None


def detect_installation():
    """Identify source, legacy Inno, Velopack, or an unknown frozen build.

    Layout and package identity are identification only, not update trust evidence.
    """
    if not getattr(sys, 'frozen', False):
        return Installation('source')
    if sys.platform != 'win32':
        return Installation('unknown_frozen')
    directory = Path(sys.executable).parent
    inno = directory / 'unins000.exe'
    if inno.is_file():
        return Installation('inno', directory, inno)
    updater = directory.parent / 'Update.exe'
    manifest = directory / 'sq.version'
    try:
        if directory.name.lower() != 'current' or not updater.is_file():
            return Installation('unknown_frozen')
        if manifest.stat().st_size > 65536:
            return Installation('unknown_frozen')
        data = manifest.read_bytes()
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            return Installation('unknown_frozen')
        xml = ET.fromstring(data)
        def local(tag):
            return tag.rsplit('}', 1)[-1]
        metadata = [child for child in xml if local(child.tag) == 'metadata']
        ids = [child.text for section in metadata for child in section if local(child.tag) == 'id']
        if local(xml.tag) != 'package' or len(metadata) != 1 or ids != [PACKAGE_ID]:
            return Installation('unknown_frozen')
    except (OSError, ET.ParseError, ValueError, TypeError):
        return Installation('unknown_frozen')
    return Installation('velopack', directory.parent, updater)


def initialize_velopack(argv=None):
    """Run early startup; return True for lifecycle calls if the SDK returns.

    argv, when provided, has the same shape as sys.argv, including executable.
    """
    args = list(sys.argv if argv is None else argv)[1:]
    lifecycle = bool(args and args[0].lower() in _LIFECYCLE)
    # VPK probes the executable before it has a current/sq.version layout.
    probe = lifecycle and getattr(sys, 'frozen', False) and sys.platform == 'win32'
    if (detect_installation().kind != 'velopack' and not probe) or (args and args[0] in _WORKERS):
        return False
    import velopack
    app = velopack.App().set_auto_apply_on_startup(False)
    if argv is not None:
        app = app.set_args(args)
    app.run()
    # Normally the SDK exits internally; also stop startup in its debug mode.
    return lifecycle


def prepare_packaged_startup(argv=None):
    """Prepare stable stores before importing services that capture their paths."""
    global _application_lease
    args = list(sys.argv if argv is None else argv)[1:]
    worker = bool(args and args[0] in _WORKERS)
    installation = detect_installation()
    # Release-pinned NVIDIA workers are outside the replaceable application
    # directory. They still own the same lifetime lease as desktop workers.
    external_worker = worker and getattr(sys, 'frozen', False) and sys.platform == 'win32'
    if installation.kind != 'velopack' and not external_worker:
        if (getattr(sys, 'frozen', False) and sys.platform == 'win32'
                and Path(sys.executable).parent.name.casefold() == 'current'):
            raise RuntimeError('The TEMPO installation is incomplete. Run the retained TEMPORepair companion before starting it.')
        return
    from tempo_config import get_config_dir, get_base_dir
    from tempo_update_lock import application_lock, startup_guard
    profile = Path(get_config_dir()).parent
    with startup_guard(profile):
        if not worker:
            from tempo_update_bootstrap import bootstrap_packaged_storage
            with application_lock(profile):
                bootstrap_packaged_storage(get_base_dir(), profile)
        _application_lease = application_lock(profile, shared=True)
        _application_lease.__enter__()
    atexit.register(_application_lease.__exit__, None, None, None)
