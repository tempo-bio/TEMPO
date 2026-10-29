"""Rebuild Windows shell assets from the unchanged TEMPO logo using native Qt."""
import argparse
from pathlib import Path
import struct

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
from PyQt6.QtGui import QImage

from tempo_app_icon import ASSETS, WINDOWS_ICON_RECT, WINDOWS_ICON_SIZES, render_app_icon


def png_bytes(image):
    # A QGuiApplication can change Qt's default image DPI. Shell assets must
    # rebuild identically whether generated headlessly or during a Qt test run.
    image.setDotsPerMeterX(3780)
    image.setDotsPerMeterY(3780)
    data = QByteArray()
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, 'PNG'):
        raise RuntimeError('The shell icon PNG could not be encoded.')
    buffer.close()
    return bytes(data)


def build_icons(output=ASSETS):
    """Encode each Windows size directly from the original approved framing."""
    output = Path(output)
    source = QImage(str(ASSETS / 'tempo_icon.png'))
    png = png_bytes(render_app_icon(source, WINDOWS_ICON_RECT.width(), 'win32'))
    frames = [(size, png_bytes(render_app_icon(source, size, 'win32')))
              for size in WINDOWS_ICON_SIZES]
    offset = 6 + 16 * len(frames)
    directory, payloads = [], []
    for size, payload in frames:
        # Windows Vista and later accept full-alpha PNG data in ICO entries.
        side = 0 if size == 256 else size
        directory.append(struct.pack('<BBBBHHII', side, side, 0, 0, 1, 32, len(payload), offset))
        payloads.append(payload)
        offset += len(payload)
    ico = struct.pack('<HHH', 0, 1, len(frames)) + b''.join(directory) + b''.join(payloads)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'tempo_shell_icon.png').write_bytes(png)
    (output / 'tempo_icon.ico').write_bytes(ico)
    return output / 'tempo_shell_icon.png', output / 'tempo_icon.ico'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ASSETS)
    arguments = parser.parse_args()
    for path in build_icons(arguments.output):
        print(path)


if __name__ == '__main__':
    main()
