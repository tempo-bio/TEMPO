"""Bounded investigation fixtures. All device readings in this module are simulated."""
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
from tempo_config import DEFAULT_PREFERENCES
from tempo_stimulation import PacerStatus, StimulusParameters
from tests.test_stimulation_investigation import CaptureService, raw_row, result


class InvestigationService(CaptureService):
    def __init__(self, protocol=1):
        super().__init__()
        self.investigation_protocol = protocol
        self.running = True
        self.error = ''

    def submit(self, command):
        if command.startswith('si1 '): return super().submit(command)
        self.commands.append(command)
        mode = self.status.mode
        parameters = self.status.parameters
        if command == 'stop': mode = 'REST_DISCHARGE'
        elif command.startswith('set '):
            _, voltage, frequency, width, channel, confirmed = command.split()
            parameters = StimulusParameters(float(voltage), float(frequency), float(width), int(channel))
        elif command == 'pace': mode = 'PACING'
        self.status = PacerStatus(mode, parameters, False, 0, 2000, 4.77)
        return result(self.status)


@contextmanager
def investigation_view(protocol=1):
    from tempo_ui import install_ui
    from tempo_tasks import dispose_closed_views
    app = QApplication.instance() or QApplication([])
    install_ui(app)
    with patch('platform.machine', return_value='x86_64'):
        import tempo_gui as gui
    with patch('tempo_capture_ui.CaptureTools.showEvent', lambda self, event: None), \
         patch.object(gui.tempo_preferences, '_preferences', deepcopy(DEFAULT_PREFERENCES)):
        view = gui.CaptureTab()
        view.capture_controls._profile_loaded = True
        view.capture_controls.service = InvestigationService(protocol)
        view.capture_mode.setCurrentIndex(1)
        view.show()
        for _ in range(8): app.processEvents()
        try:
            yield view
        finally:
            workspace = view.investigation_workspace
            if workspace:
                workspace.timer.stop()
            view.timer.stop()
            view.capture_controls.poll_timer.stop()
            view.capture_controls.focus_warning_timer.stop()
            view.capture_controls.service = None
            view.close()
            dispose_closed_views(view)


def populate(workspace, volume=8):
    condition = workspace.investigation.add_condition('M1 · 3 mm · 0.6 V')
    condition.update(well='A01', medium='DMEM, batch 24A', fill_volume_ul='200',
        commanded_voltage_v='0.6', frequency_hz='1', pulse_width_ms='2', output='1',
        electrode_material='Carbon', electrode_dimensions='Two strips, 5 × 12 mm',
        electrode_position_well='Opposite walls, 1 mm inward', wire_material='Platinum',
        wire_dimensions='Two wires, 0.25 mm diameter', wire_position_well='Centered; 3 mm apart',
        wire_position_electrodes='Parallel, midway between strips')
    for index in range(volume):
        scope = workspace.investigation.add_record(condition['id'], 'oscilloscope', f'Run {index + 1}')
        scope['values'] = dict(instrument='Bench scope / 1×', measurement_window='Middle of pulse',
            baseline_ch1_v='0.003', baseline_ch2_v='0.002', pulse_ch1_v='0.313', pulse_ch2_v='0.018',
            difference_v='0.295', pulse_width_ms='2.01', time_per_division='2 ms',
            volts_per_division='0.2 V', probe_attenuation='1×', coupling='DC')
        scope['notes'] = 'Stable plateau; readings entered manually.'
    other = workspace.investigation.duplicate_condition(condition['id'])
    other['name'] = 'M1 · 6 mm · 0.6 V'; other['wire_position_well'] = 'Centered; 6 mm apart'
    workspace.load_conditions(condition['id'])
    workspace.changed()
    return condition
