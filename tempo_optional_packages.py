"""Install approved TEMPO add-ons into the user profile without rewriting the application."""
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import zipimport

from tempo_release import APP_DISPLAY_NAME


@dataclass(frozen=True)
class Package:
    name: str
    api: int
    archives: dict[str, str]


INVESTIGATION = 'stimulation_investigation_1'
# Preserve previously approved hashes while the corresponding API is supported.
PACKAGES = {
    INVESTIGATION: Package('Stimulation Investigation 1', 1, {
        '0.2.0': '7347fd3f9e85de6c2bfa5ec32e5a5a85f916e9d93784edaf44272e451cded384',
        '0.2.1': 'c9824af94e08b2876cdeec3bcde0a497c3b0b009745a51503cfb14adb47e8e08',
        '0.2.2': 'a76b343122ee1d56b53b2e7710fe4d2b094140fe10662baea06272d378d6a9a8',
    }),
}
MAX_PACKAGE_BYTES = 2 * 1024 * 1024
SUPPORTED_APIS = {1}
_loaded_packages = {}


def package_root():
    from tempo_config import get_config_dir
    return Path(get_config_dir()).parent / 'optional-packages'


def archive_path(package_id, root=None):
    if package_id not in PACKAGES:
        raise ValueError('Unknown TEMPO package.')
    return (package_root() if root is None else Path(root)) / (package_id + '.zip')


def is_installed(package_id, root=None):
    return archive_path(package_id, root).is_file()


def _read(path):
    with Path(path).open('rb') as stream:
        payload = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(payload) > MAX_PACKAGE_BYTES:
        raise ValueError('The optional package exceeds the supported size.')
    return payload


def _identify(payload):
    digest = hashlib.sha256(payload).hexdigest()
    for package_id, package in PACKAGES.items():
        for version, approved in package.archives.items():
            if digest == approved:
                if package.api not in SUPPORTED_APIS:
                    raise ValueError('This TEMPO build does not support the package interface.')
                return package_id, version
    raise ValueError('Choose an approved TEMPO package ZIP for this build.')


def install(source, root=None):
    payload = _read(source)
    package_id, version = _identify(payload)
    destination = archive_path(package_id, root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return package_id, version


def remove(package_id, root=None):
    archive_path(package_id, root).unlink(missing_ok=True)


def load(package_id, component, root=None):
    path = archive_path(package_id, root).resolve()
    if package_id in _loaded_packages:
        if _loaded_packages[package_id][0] != path:
            raise ImportError('A different copy of this TEMPO package is already loaded.')
    else:
        if package_id in sys.modules:
            raise ImportError('A different copy of this TEMPO package is already loaded.')
        payload = _read(path)
        if _identify(payload)[0] != package_id:
            raise ValueError('The installed file belongs to a different TEMPO package.')
        # Use an immutable copy for this process. Install and removal take effect
        # at the next launch, even when the existing mode is open.
        session = tempfile.TemporaryDirectory(prefix='tempo-optional-')
        try:
            archive = Path(session.name) / path.name
            archive.write_bytes(payload)
            spec = zipimport.zipimporter(str(archive)).find_spec(package_id)
            if spec is None:
                raise ImportError('The approved package is missing its entry point.')
            module = importlib.util.module_from_spec(spec)
            sys.modules[package_id] = module
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(package_id, None)
            session.cleanup()
            raise
        _loaded_packages[package_id] = (path, session)
    return importlib.import_module(f'{package_id}.{component}')


def show_package_manager(parent):
    from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QLabel, QListWidget,
                                 QListWidgetItem, QVBoxLayout)
    from PyQt6.QtCore import Qt

    dialog = QDialog(parent)
    dialog.setWindowTitle(f'{APP_DISPLAY_NAME} · Optional Packages')
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    layout = QVBoxLayout(dialog)
    listing = QListWidget()
    listing.setAccessibleName('Optional packages')
    layout.addWidget(listing)
    status = QLabel()
    status.setWordWrap(True)
    status.hide()
    layout.addWidget(status)
    actions = QDialogButtonBox()
    install_button = actions.addButton('Install Package…', QDialogButtonBox.ButtonRole.ActionRole)
    remove_button = actions.addButton('Remove', QDialogButtonBox.ButtonRole.ActionRole)
    layout.addWidget(actions)

    def refresh():
        current = listing.currentRow()
        listing.clear()
        for package_id, package in PACKAGES.items():
            state = 'Installed' if is_installed(package_id) else 'Available'
            item = QListWidgetItem(f'{package.name} · {state}')
            item.setData(Qt.ItemDataRole.UserRole, package_id)
            listing.addItem(item)
        listing.setCurrentRow(max(0, current))

    def update_actions():
        item = listing.currentItem()
        remove_button.setEnabled(bool(item and is_installed(item.data(Qt.ItemDataRole.UserRole))))

    def show_status(text):
        status.setText(text)
        status.setVisible(bool(text))

    def choose_package():
        filename, _ = QFileDialog.getOpenFileName(dialog, 'Install Optional Package', '', 'TEMPO packages (*.zip)')
        if filename:
            try:
                install(filename)
                refresh()
                show_status(f'Restart {APP_DISPLAY_NAME} to use the installed package.')
            except (OSError, ValueError) as error:
                show_status(str(error))

    def remove_package():
        item = listing.currentItem()
        if item:
            try:
                remove(item.data(Qt.ItemDataRole.UserRole))
                refresh()
                show_status(f'Restart {APP_DISPLAY_NAME} to finish removing the package.')
            except OSError as error:
                show_status(str(error))

    listing.currentItemChanged.connect(update_actions)
    install_button.clicked.connect(choose_package)
    remove_button.clicked.connect(remove_package)
    refresh()
    dialog.resize(420, 230)
    return dialog
