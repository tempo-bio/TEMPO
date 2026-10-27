"""Workspace-guide chrome and optional, bundled onboarding documentation."""
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QKeySequence, QPalette, QShortcut, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (QBoxLayout, QDialog, QFrame, QHBoxLayout, QLabel,
                            QPushButton, QTextBrowser, QVBoxLayout, QWidget)

from tempo_logo import AnimatedTempoWordmark


GUIDES = {
    'start': ('Getting started', 'docs/GETTING_STARTED.md'),
    'account': ('Account and access', 'docs/GETTING_STARTED.md#connect-your-workspace'),
    'models': ('Models and hardware', 'docs/GETTING_STARTED.md#models-and-hardware'),
}


def link_button(text, callback):
    button = QPushButton(text)
    button.setProperty('onboardingLink', True)
    button.setAutoDefault(False)
    button.clicked.connect(callback)
    return button


class SetupGuide(QDialog):
    """Nonmodal reading stays available while protected installation runs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resize(680, 600)
        self.root = Path(__file__).resolve().parent
        self.current_path = self.root / 'docs/GETTING_STARTED.md'
        layout = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        palette = self.browser.palette()
        palette.setColor(QPalette.ColorRole.Link, QColor('#79c0ff'))
        palette.setColor(QPalette.ColorRole.LinkVisited, QColor('#a8cfff'))
        self.browser.setPalette(palette)
        self.browser.document().setDefaultStyleSheet('a { color: #79c0ff; }')
        self.browser.anchorClicked.connect(self.follow_link)
        layout.addWidget(self.browser)
        self._close_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Close), self)
        self._close_shortcut.activated.connect(self.close)

    def open_guide(self, key):
        title, path = GUIDES[key]
        self.setWindowTitle(title)
        self.follow_link(QUrl.fromLocalFile(str(self.root / path.split('#')[0])))
        self.show()
        self.raise_()
        if '#' in path:
            self._scroll_to_section(path.split('#')[1])

    def follow_link(self, url):
        if url.scheme() in ('https', 'http'):
            QDesktopServices.openUrl(url)
            return
        if url.scheme() not in ('', 'file'):
            return
        if not url.path():
            self._scroll_to_section(url.fragment())
            return
        path = (Path(url.toLocalFile()) if url.isLocalFile()
                else self.current_path.parent / url.path()).resolve()
        if path.suffix.lower() != '.md' or not (path.is_relative_to(self.root / 'docs') or path == self.root / 'README.md'):
            return
        try:
            content = path.read_text(encoding='utf-8')
        except OSError:
            self.browser.setPlainText('This guide is unavailable. Reinstall TEMPO to restore the bundled documentation.')
            return
        self.current_path = path
        self.browser.document().setBaseUrl(QUrl.fromLocalFile(str(path.parent) + '/'))
        self.browser.setMarkdown(content)
        # Markdown assigns explicit blue foregrounds, ignoring the link palette.
        document = self.browser.document()
        anchors = []
        block = document.begin()
        while block.isValid():
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid() and fragment.charFormat().isAnchor():
                    anchors.append((fragment.position(), fragment.length()))
                iterator += 1
            block = block.next()
        style = QTextCharFormat()
        style.setForeground(QColor('#79c0ff'))
        style.setFontUnderline(True)
        cursor = QTextCursor(document)
        for position, length in anchors:
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(style)
        if url.fragment():
            self._scroll_to_section(url.fragment())

    def _scroll_to_section(self, fragment):
        # QTextDocument's Markdown reader does not generate HTML heading IDs.
        cursor = self.browser.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        if self.browser.find(fragment.replace('-', ' ')):
            cursor = self.browser.textCursor()
            cursor.clearSelection()
            self.browser.setTextCursor(cursor)
            self.browser.ensureCursorVisible()
            bar = self.browser.verticalScrollBar()
            bar.setValue(bar.value() + self.browser.cursorRect().top() - 8)
        else:
            self.browser.scrollToAnchor(fragment)


class WorkspaceGuideShell(QWidget):
    compact_changed = pyqtSignal(bool)

    def __init__(self, main, open_guide, parent=None):
        super().__init__(parent)
        self.layout_box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.layout_box.setSpacing(0)
        self.rail = QFrame()
        self.rail.setObjectName('setupRail')
        rail = QVBoxLayout(self.rail)
        self.rail_box = rail
        rail.setContentsMargins(20, 24, 20, 22)
        rail.setSpacing(26)
        self.logo = AnimatedTempoWordmark()
        rail.addWidget(self.logo)
        self.steps = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self.steps.setSpacing(20)
        self.labels = []
        for number, name in enumerate(('App', 'Account', 'Models'), 1):
            label = QLabel(f'{number}  {name}')
            label.setWordWrap(True)
            self.labels.append(label)
            self.steps.addWidget(label)
        rail.addLayout(self.steps)
        rail.addStretch()
        self.docs = QWidget()
        links = QVBoxLayout(self.docs)
        links.setContentsMargins(0, 0, 0, 0)
        links.setSpacing(12)
        for key, (title, _) in GUIDES.items():
            links.addWidget(link_button(title, lambda checked=False, key=key: open_guide(key)),
                            alignment=Qt.AlignmentFlag.AlignLeft)
        rail.addWidget(self.docs)
        self.layout_box.addWidget(self.rail)
        self.layout_box.addWidget(main, 1)
        self.narrow = None

    def set_stage(self, stage):
        for index, label in enumerate(self.labels):
            caption = ('App', 'Account', 'Models')[index]
            label.setText(f'{"✓" if index < stage else index + 1}  {caption}')
            label.setStyleSheet('color: ' + ('#7ce4ba' if index < stage else '#edf2f7' if index == stage else '#aab8c8') + ';')
            font = label.font()
            font.setBold(index == stage)
            label.setFont(font)
            label.setAccessibleName(caption + (' complete' if index < stage else ' current step' if index == stage else ''))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rail_width = max(184, self.fontMetrics().horizontalAdvance('Models and hardware') + 40)
        narrow = self.width() < rail_width + 430
        if narrow != self.narrow:
            self.narrow = narrow
            self.layout_box.setDirection(QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight)
            self.steps.setDirection(QBoxLayout.Direction.LeftToRight if narrow else QBoxLayout.Direction.TopToBottom)
            self.steps.setSpacing(12 if narrow else 20)
            self.rail_box.setContentsMargins(20, 18 if narrow else 24, 20, 14 if narrow else 22)
            self.rail_box.setSpacing(12 if narrow else 26)
            self.docs.setVisible(not narrow)
            self.compact_changed.emit(narrow)
        self.rail.setMinimumWidth(0 if narrow else rail_width)
        self.rail.setMaximumWidth(16777215 if narrow else rail_width)
