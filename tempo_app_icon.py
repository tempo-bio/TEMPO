"""Platform icon framing, separate from the original logo animation artwork."""
from pathlib import Path
import sys

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage


ASSETS = Path(__file__).resolve().parent / 'assets'
# Approved option B: the existing tile occupies approximately 96% of the slot.
# Keep the source artwork intact: its original coordinates drive the QRS trace.
WINDOWS_ICON_RECT = QRect(93, 93, 1068, 1068)
WINDOWS_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def application_icon_path(platform=None):
    platform = sys.platform if platform is None else platform
    return ASSETS / ('tempo_shell_icon.png' if platform == 'win32' else 'tempo_icon.png')


def icon_source_rect(source, platform=None):
    """Return the same framing for the static icon and every activity frame."""
    platform = sys.platform if platform is None else platform
    if source.isNull():
        raise ValueError('The TEMPO icon artwork is missing or unreadable.')
    rectangle = WINDOWS_ICON_RECT if platform == 'win32' else source.rect()
    if not source.rect().contains(rectangle):
        raise ValueError('The TEMPO icon artwork does not contain the approved framing.')
    return QRect(rectangle)


def render_app_icon(source, size, platform=None):
    if not isinstance(size, int) or size <= 0:
        raise ValueError('An icon size must be a positive number of pixels.')
    return source.copy(icon_source_rect(source, platform)).scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_ARGB32)
