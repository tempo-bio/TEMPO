"""Release fixture-owned Qt objects before interpreter shutdown."""
from time import monotonic
from PyQt6 import sip
from PyQt6.QtTest import QTest
from tempo_tasks import dispose_closed_views


def wait_for_qt(condition, timeout=2):
    """Process Qt events until a fixture's state is ready or its deadline expires."""
    deadline = monotonic() + timeout
    while not condition():
        if monotonic() >= deadline:
            return False
        QTest.qWait(10)
    return monotonic() <= deadline


def close_and_dispose(view):
    if sip.isdeleted(view):
        return
    if not view.close():
        raise RuntimeError('A fixture window refused to close; its work must settle before disposal.')
    dispose_closed_views(view)
