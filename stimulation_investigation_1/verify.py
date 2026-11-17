"""Offline release smoke check with explicitly simulated instruments."""
from concurrent.futures import Future
from pathlib import Path
import tempfile
from unittest.mock import patch


def verify_workflow():
    from PyQt6.QtWidgets import QApplication
    from tempo_ui import install_ui, prepare_offscreen_fonts
    from tempo_tasks import dispose_application, dispose_closed_views
    from tempo_stimulation import PacerStatus, StimulusParameters
    from .firmware import development_sketch
    from .storage import Investigation

    with development_sketch() as sketch:
        assert (sketch / 'investigation_1.h').is_file()
        assert (sketch / (sketch.name + '.ino')).is_file()
    checks = ['Development sketch staged from shipped resources']

    app = QApplication.instance() or QApplication([])
    prepare_offscreen_fonts(app)
    install_ui(app)
    with patch('tempo_capture_ui.CaptureTools.showEvent', lambda self, event: None):
        import tempo_gui
        view = tempo_gui.CaptureTab()
        try:
            view.capture_controls._profile_loaded = True
            class Service:
                connected = True
                investigation_protocol = 1
                firmware = 'simulated'
                port = 'simulated'
                running = False
                error = ''
                status = PacerStatus('REST_DISCHARGE', StimulusParameters(), False, 0, 2000, 4.77)
                def submit(self, command):
                    parts = command.split(); self.identifier = int(parts[2])
                    rows = [[10,20,1,1234,0,0,False,False,0,0,0,0], [30,40,2,234,0,0,False,False,0,0,0,0]]
                    offset = int(parts[3]) if parts[1] == 'fetch' else 0
                    response = dict(si1_protocol=1, si1=dict(id=self.identifier, state='complete', count=2,
                        target=2, adc_bits=14, offset=offset, samples=rows[offset:] if parts[1] == 'fetch' else []))
                    future = Future(); future.set_result(response); return future
            service = Service(); view.capture_controls.service = service
            view.capture_mode.setCurrentIndex(1)
            workspace = view.investigation_workspace
            assert workspace is not None and view.investigation_mode
            workspace.timer.stop()
            assert view.capture_splitter.widget(0) is workspace.sidebar
            assert view.capture_splitter.widget(2) is view.stimulation_panel
            checks.append('Acquisition mode and retained stimulation controls')
            workspace.add_condition()
            workspace.condition_widgets['medium'].setText('Instrument check medium')
            workspace.add_scope_record()
            workspace.scope_widgets['pulse_ch1_v'].setText('0.314 ± 0.002')
            first = workspace.selected_condition['id']
            workspace.duplicate_condition()
            assert workspace.scope_list.count() == 0
            with tempfile.TemporaryDirectory(prefix='tempo-investigation-verification-') as directory:
                path = Path(directory) / 'check.json'
                workspace.investigation.save(path); workspace.mark_saved()
                workspace.readings.setValue(2); workspace.record_arduino()
                for _ in range(10): workspace.poll()
                loaded = Investigation.open(path)
                records = loaded.document['records']
                assert len(records) == 2 and records[0]['condition_id'] == first
                assert records[1]['condition_id'] != first
                assert records[0]['values']['pulse_ch1_v'] == '0.314 ± 0.002'
                assert records[1]['capture']['state'] == 'complete'
                assert [r['raw_count'] for r in records[1]['capture']['samples']] == [1234,234]
                checks.append('Independent scope and raw Arduino records save and reopen')
                workspace.investigation.export(Path(directory) / 'check.zip')
                checks.append('Portable data export')
            view.capture_mode.setCurrentIndex(0)
            assert view.capture_splitter.widget(0) is view.camera_panel
        finally:
            if view.investigation_workspace is not None:
                view.investigation_workspace.timer.stop()
            view.capture_controls.service = None
            view.capture_controls.poll_timer.stop()
            view.capture_controls.focus_warning_timer.stop()
            view.close()
            dispose_closed_views(view)
            dispose_application(app)
    checks.append('Imaging restored and Qt resources disposed')
    return dict(checks=checks, simulated_instruments=True, hardware_tested=False)
