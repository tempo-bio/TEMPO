"""Render simulated update-dialog alternatives without a native window.

Run: python -m scripts.render_update_choices [output-directory]
"""
import argparse
import os
from pathlib import Path

# Must be set before any Qt import; previews never contact an update service.
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel,
                             QProgressBar, QPushButton, QTextEdit, QVBoxLayout)
from tempo_ui import install_ui

NOTES = ('## What’s new\n\n'
         'This sample release includes improvements to analysis, recording, and reliability.\n\n'
         '- Review results more easily.\n'
         '- Improved handling of interrupted work.\n'
         '- Fixes for common installation issues.')


def make_dialog(option, state):
    dialog = QDialog()
    dialog.setWindowTitle('Software Update')
    layout = QVBoxLayout(dialog)
    if option == 'b':
        version = QLabel('<b>TEMPO 1.0.2</b>')
        summary = QLabel('Update download: 4.3 MB')
        summary.setWordWrap(True)
        layout.addWidget(version)
        layout.addWidget(summary)
    notes = QTextEdit()
    notes.setReadOnly(True)
    notes.setTabChangesFocus(True)
    if option == 'a':
        notes.setMarkdown('## TEMPO 1.0.2\n\nUpdate download: 4.3 MB\n\n' + NOTES)
    else:
        notes.setMarkdown(NOTES)
    layout.addWidget(notes, 1)
    if state == 'downloading':
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(42)
        progress.setFormat('Downloading: %p%')
        layout.addWidget(progress)
    elif state == 'low-space':
        error = QLabel('Not enough disk space to prepare the update. Free space and check again.')
        error.setWordWrap(True)
        error.setStyleSheet('color: #F2994A;')
        layout.addWidget(error)
    row = QHBoxLayout()
    retry = QPushButton('Check Again')
    retry.setEnabled(state != 'downloading')
    row.addWidget(retry)
    row.addStretch()
    action = QPushButton('Install and Restart' if state == 'ready' else
                         'Cancel Download' if state == 'downloading' else 'Download Update')
    action.setProperty('primary', True)
    action.setEnabled(state != 'low-space')
    row.addWidget(action)
    layout.addLayout(row)
    return dialog, (retry, action)


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    install_ui(app, theme_name='dark')
    sizes = {'ordinary': (620, 420, 12), 'small': (520, 360, 12),
             'large-font': (620, 420, 18)}
    states = ('available', 'downloading', 'ready', 'low-space')
    for variant, (width, height, points) in sizes.items():
        app.setFont(QFont('Arial', points))
        for state in states:
            images = []
            for option in ('a', 'b'):
                dialog, buttons = make_dialog(option, state)
                dialog.resize(width, height)
                dialog.show()
                for _ in range(4):
                    app.processEvents()
                if dialog.size().width() != width or dialog.size().height() != height:
                    raise RuntimeError(f'{variant}/{state}/{option}: window exceeded requested size')
                for button in buttons:
                    if not dialog.rect().contains(button.mapTo(dialog, button.rect().topLeft())) or \
                       button.width() < button.fontMetrics().horizontalAdvance(button.text()) + 10:
                        raise RuntimeError(f'{variant}/{state}/{option}: button is clipped')
                path = output / f'{variant}-{state}-{option}.png'
                dialog.grab().save(str(path))
                images.append(QImage(str(path)))
                dialog.close()
                dialog.deleteLater()
                app.processEvents()
            # Side-by-side image uses identical content, dimensions, and state.
            canvas = QImage(width * 2 + 16, height + 40, QImage.Format.Format_ARGB32)
            canvas.fill(QColor('#171b20'))
            painter = QPainter(canvas)
            painter.setPen(QColor('#f1f4f7'))
            painter.setFont(QFont('Arial', 12))
            painter.drawText(12, 26, 'A · Release notes')
            painter.drawText(width + 28, 26, 'B · Summary above release notes')
            painter.drawImage(0, 40, images[0])
            painter.drawImage(width + 16, 40, images[1])
            painter.end()
            canvas.save(str(output / f'{variant}-{state}-comparison.png'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', nargs='?', type=Path,
                        default=Path('.agent-artifacts/update-ui-choices'))
    render(parser.parse_args().output)
