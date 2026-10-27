"""Release fixture-owned Qt objects before interpreter shutdown."""
from PyQt6 import sip
from tempo_tasks import dispose_closed_views


def close_and_dispose(view):
    if sip.isdeleted(view):
        return
    if not view.close():
        raise RuntimeError('A fixture window refused to close; its work must settle before disposal.')
    dispose_closed_views(view)
