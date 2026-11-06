"""Read-only identification and early startup for Windows installations."""
from dataclasses import dataclass
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

PACKAGE_ID = 'TEMPO.Bio.Desktop'
_LIFECYCLE = frozenset(('--veloapp-install', '--veloapp-updated',
                        '--veloapp-obsolete', '--veloapp-uninstall'))
_WORKERS = frozenset(('--assist-model', '--assist-model-cuda', '--probe-cuda',
                      '--predict-cells', '--predict-nuclei', '--verify-installation',
                      '--verify-probe'))


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
    if detect_installation().kind != 'velopack' or (args and args[0] in _WORKERS):
        return False
    import velopack
    app = velopack.App().set_auto_apply_on_startup(False)
    if argv is not None:
        app = app.set_args(args)
    app.run()
    # Normally the SDK exits internally; also stop startup in its debug mode.
    return bool(args and args[0].lower() in _LIFECYCLE)
