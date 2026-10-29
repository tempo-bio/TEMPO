"""Per-user packaged onboarding state, independent of the installed payload."""
import json
import os
from pathlib import Path
import sys
import tempfile

from tempo_config import get_config_dir

_STAGES = frozenset(('welcome', 'account', 'models', 'complete'))


def setup_state_path():
    return Path(get_config_dir()) / 'setup_state.json'


def setup_stage():
    """Missing or incomplete state resumes setup; saved account values stay intact."""
    try:
        state = json.loads(setup_state_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 'welcome'
    if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
            or state['schema_version'] != 1 or not isinstance(state.get('stage'), str)
            or state['stage'] not in _STAGES):
        return 'welcome'
    return state['stage']


def save_setup_stage(stage):
    if not isinstance(stage, str) or stage not in _STAGES:
        raise ValueError('Unknown setup stage.')
    # Opening Guided Setup later must not make an established installation new.
    if stage != 'complete' and setup_stage() == 'complete':
        return
    path = setup_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix='setup_state.', suffix='.tmp', dir=path.parent)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(dict(schema_version=1, stage=stage), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def run_packaged_setup(application):
    """Keep one QApplication alive across onboarding and the desktop handoff."""
    if not getattr(sys, 'frozen', False) or setup_stage() == 'complete':
        return True
    from PyQt6.QtWidgets import QDialog
    from setup_wizard import SetupWizardDialog
    from tempo_tasks import dispose_closed_views
    previous = application.quitOnLastWindowClosed()
    application.setQuitOnLastWindowClosed(False)
    wizard = SetupWizardDialog()
    try:
        accepted = wizard.exec() == QDialog.DialogCode.Accepted
    finally:
        dispose_closed_views(wizard)
        application.setQuitOnLastWindowClosed(previous)
    # A closed or programmatically accepted dialog is not a completion record.
    return accepted and setup_stage() == 'complete'
