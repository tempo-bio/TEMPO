"""Render simulated collection states offscreen; never start native TEMPO or hardware.

Run: python -m scripts.render_stimulation_investigation_screens
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def render(output):
    output.mkdir(parents=True, exist_ok=True)
    screen = output / 'screen.json'
    screen.write_text(json.dumps({'screens': [{'name': 'Investigation review', 'width': 4096, 'height': 2304}]}))
    os.environ['QT_QPA_PLATFORM'] = 'offscreen:configfile=' + str(screen.resolve())
    os.environ['MPLCONFIGDIR'] = str((output / 'matplotlib').resolve())
    from PyQt6.QtWidgets import QApplication, QPushButton, QLabel
    from PyQt6.QtGui import QFont
    from PyQt6.QtCore import Qt, QPoint, QRect
    from PyQt6.QtTest import QTest
    from tempo_ui import help_controller
    from tempo_tasks import dispose_application
    from tests.investigation_fixtures import investigation_view, populate
    from tests.test_stimulation_investigation import raw_row
    app = QApplication.instance() or QApplication([])
    errors = []
    original_hook = sys.excepthook
    sys.excepthook = lambda *error: errors.append(str(error[1]))
    records = []
    cases = [('scope', (1340, 900), 12, 8), ('wide', (1680, 980), 12, 8),
             ('small', (980, 680), 12, 8), ('large-font', (1280, 900), 18, 8),
             ('empty', (1180, 780), 12, 0), ('long-history', (1340, 900), 12, 150),
             ('unavailable', (1180, 780), 12, 0), ('arduino-only', (1340, 900), 12, 0),
             ('active', (1180, 780), 12, 0)]
    try:
        with tempfile.TemporaryDirectory(prefix='tempo-investigation-render-') as directory:
            for name, size, font, volume in cases:
                app.setFont(QFont('Arial', font))
                with investigation_view(protocol=None if name == 'unavailable' else 1) as view:
                    workspace = view.investigation_workspace
                    workspace.timer.stop()
                    view.resize(*size)
                    if name != 'empty': populate(workspace, volume)
                    if name in ('arduino-only', 'active'):
                        workspace.investigation.save(Path(directory) / (name + '.json'))
                        workspace.mark_saved()
                        if name == 'active':
                            workspace.trigger.setCurrentIndex(1)
                            view.capture_controls.service.submit('pace')
                            view.capture_controls.service.state = 'armed'
                        workspace.record_arduino()
                        service = view.capture_controls.service
                        if name == 'arduino-only':
                            service.rows = [raw_row(i) for i in range(64)]
                            service.state = 'complete'
                            for _ in range(15): workspace.poll()
                        else:
                            service.state = 'armed'; workspace.poll(); workspace.poll()
                        workspace.tabs.setCurrentIndex(1)
                    if name == 'unavailable': workspace.tabs.setCurrentIndex(1)
                    workspace.refresh_device()
                    for _ in range(12): app.processEvents()
                    view.grab().save(str(output / (name + '.png')))
                    stop = view.capture_controls.stop_button
                    bounds = QRect(stop.mapTo(view, QPoint()), stop.size())
                    assert view.rect().contains(bounds), f'{name}: Stop Stimulation outside view'
                    assert stop.isVisible() and view.stimulation_panel.isAncestorOf(stop)
                    assert view.capture_splitter.widget(0) is workspace.sidebar
                    assert view.capture_splitter.widget(1) is workspace
                    assert view.capture_splitter.widget(2) is view.stimulation_panel
                    assert view.width() == size[0], f'{name}: forced width {view.width()}'
                    for button in (workspace.save_button, workspace.add_condition_button,
                                   workspace.record_button, workspace.copy_settings_button, stop):
                        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 10, f'{name}: clipped {button.text()}'
                    if workspace.setup_scroll.isVisible():
                        assert workspace.sidebar.rect().contains(workspace.setup_scroll.geometry())
                        for label in workspace.setup_form.findChildren(QLabel):
                            if label.text():
                                assert label.width() > 10, f'{name}: collapsed label {label.text()}'
                                assert label.height() >= label.heightForWidth(label.width()), f'{name}: clipped label {label.text()}'
                    if name == 'long-history':
                        assert workspace.scope_list.verticalScrollBar().maximum() > 0
                    if name == 'scope':
                        workspace.tabs.setCurrentIndex(1)
                        for _ in range(6): app.processEvents()
                        icon = workspace.raw_info
                        QTest.mouseMove(view, QPoint(2, 2)); QTest.qWait(60)
                        QTest.mouseMove(icon, icon.rect().center()); QTest.qWait(240)
                        controller = help_controller()
                        assert controller.source is icon and controller.bubble is not None
                        view.grab().save(str(output / 'raw-hover.png'))
                        bubble = controller.bubble
                        QTest.mouseMove(bubble, bubble.rect().center()); QTest.qWait(200)
                        assert controller.bubble is bubble
                        QTest.mouseMove(view, QPoint(2, 2)); QTest.qWait(230)
                        assert controller.bubble is None
                        QTest.mouseClick(icon, Qt.MouseButton.LeftButton)
                        assert controller.bubble is not None
                        QTest.mouseMove(view, QPoint(2, 2)); QTest.qWait(30)
                        QTest.keyClick(icon, Qt.Key.Key_Escape)
                        assert controller.bubble is None
                        icon.setFocus(); QTest.keyClick(icon, Qt.Key.Key_Space)
                        assert controller.bubble is not None and icon.hasFocus()
                        QTest.keyClick(icon, Qt.Key.Key_Escape)
                        workspace.show_recorded_setup(workspace.scope_record)
                        for _ in range(4): app.processEvents()
                        workspace._snapshot_dialog.grab().save(str(output / 'recorded-setup.png'))
                        workspace._snapshot_dialog.close()
                        workspace.show_guide()
                        for _ in range(4): app.processEvents()
                        workspace._guide_dialog.grab().save(str(output / 'guide.png'))
                        QTest.keyClick(workspace._guide_dialog, Qt.Key.Key_Escape)
                        assert not workspace._guide_dialog.isVisible()
                    if workspace.capturing:
                        workspace.capture.cancel()
                        for _ in range(5): workspace.poll()
                    records.append(dict(name=name, size=list(size), font=font,
                        panels=view.capture_splitter.sizes(), setup_scroll=workspace.setup_scroll.verticalScrollBar().maximum(),
                        scope_scroll=workspace.scope_scroll.verticalScrollBar().maximum(), stop_visible=True))
            assert not errors, errors
    finally:
        sys.excepthook = original_hook
        dispose_application(app)
    (output / 'geometry.json').write_text(json.dumps(records, indent=2) + '\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('.agent-artifacts/stimulation-investigation-1/screens'))
    args = parser.parse_args()
    print(json.dumps(render(args.output), indent=2))
