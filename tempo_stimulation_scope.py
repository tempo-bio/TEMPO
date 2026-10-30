"""Analytic oscilloscope preview of commanded stimulation, never a measurement."""
import math
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QPushButton, QLabel, QGroupBox, QSizePolicy, QTextBrowser)
from tempo_ui import InformationButton, FlowRow, get_current_theme

TIME_DIVISIONS = (.00002,.00005,.0001,.0002,.0005,.001,.002,.005,.01,.02,.05,.1,.2,.5,1,2,5,10)
VOLT_DIVISIONS = (.01,.02,.05,.1,.2,.5,1,2)


def recommended_scales(voltage, frequency, width, mode='pace', view='pulse'):
    if mode == 'constant':
        span = .1
    elif view == 'train':
        span = 1 / frequency / 5
    else:
        span = width / 2
    time_div = next((n for n in TIME_DIVISIONS if n >= span), TIME_DIVISIONS[-1])
    volt_div = next((n for n in VOLT_DIVISIONS if n >= voltage/4), VOLT_DIVISIONS[-1])
    return time_div,volt_div


def time_label(seconds):
    if seconds < .001: return f'{seconds*1e6:g} µs/div'
    if seconds < 1: return f'{seconds*1000:g} ms/div'
    return f'{seconds:g} s/div'


def waveform_segments(voltage, frequency, width, mode, start, end):
    """Exact edge positions prevent a narrow pulse disappearing between samples."""
    if mode == 'constant': return [(start,voltage),(end,voltage)]
    period = 1/frequency
    first,last = math.floor(start/period),math.ceil(end/period)
    # Protect painting at very wide custom scales; display an envelope below.
    if last-first > 2000: return None
    result = [(start,voltage if start % period < width else 0)]
    for beat in range(first,last+1):
        rising = beat*period
        falling = rising+width
        if start < rising < end: result.extend(((rising,0),(rising,voltage)))
        if start < falling < end: result.extend(((falling,voltage),(falling,0)))
    result.append((end,voltage if end % period < width else 0))
    return result


class ScopeTrace(QWidget):
    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.voltage,self.frequency,self.pulse_width_s,self.mode = .6,1.,.002,'pace'
        self.time_div,self.volt_div = .001,.2
        self.setMinimumHeight(max(240, self.fontMetrics().height() * 9) if compact else 230)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumWidth(180)
        self.setAccessibleName('Simulated stimulation waveform')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.compact:
            # Preserve the scope's 10:8 graticule rather than stretching a
            # narrow sidebar trace into a tall, misleading grid.
            height = round(max(0,self.width()-16)*.8) + self.fontMetrics().height() + 14
            self.setMaximumHeight(max(self.minimumHeight(),height))

    def paintEvent(self,event):
        t = get_current_theme()
        painter = QPainter(self)
        painter.fillRect(self.rect(),QColor(t.get('scope_bg', '#101820')))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = painter.font()
        if self.compact: font.setPointSizeF(max(8,font.pointSizeF()-1))
        painter.setFont(font)
        line_height = painter.fontMetrics().height()
        plot = QRectF(8,6,self.width()-16,max(20,self.height()-line_height-14))
        painter.setPen(QPen(QColor(t.get('scope_grid', '#2c3d4d')),1))
        for i in range(11):
            x = plot.left()+plot.width()*i/10
            painter.drawLine(int(x),int(plot.top()),int(x),int(plot.bottom()))
        for i in range(9):
            y = plot.top()+plot.height()*i/8
            painter.drawLine(int(plot.left()),int(y),int(plot.right()),int(y))
        baseline = plot.bottom()-plot.height()/8
        y_value = lambda value: baseline-value/self.volt_div*plot.height()/8
        painter.setPen(QPen(QColor(t.get('scope_baseline', '#73879a')),1))
        painter.drawLine(int(plot.left()),int(baseline),int(plot.right()),int(baseline))
        if self.mode == 'isolated':
            painter.setPen(QColor(t.get('scope_text', '#d3dfeb')))
            painter.drawText(plot,Qt.AlignmentFlag.AlignCenter|Qt.TextFlag.TextWordWrap,'Isolated · voltage is not predicted')
        elif self.pulse_width_s*self.frequency >= 1 and self.mode != 'constant':
            painter.setPen(QColor(t.get('scope_warn', '#f2b36f')))
            painter.drawText(plot,Qt.AlignmentFlag.AlignCenter,'Width must be shorter than the period')
        else:
            start,end = -self.time_div,9*self.time_div
            points = waveform_segments(self.voltage,self.frequency,self.pulse_width_s,self.mode,start,end)
            painter.save(); painter.setClipRect(plot)
            painter.setPen(QPen(QColor(t.get('scope_trace', '#65dfa5')),1.8))
            if points is None:
                trace_color = QColor(t.get('scope_trace', '#65dfa5'))
                trace_color.setAlpha(65)
                painter.fillRect(QRectF(plot.left(),y_value(self.voltage),plot.width(),
                    baseline-y_value(self.voltage)), trace_color)
                painter.drawText(plot,Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignHCenter,'Pulses overlap at this scale')
            else:
                path = QPainterPath()
                for index,(t_val,value) in enumerate(points):
                    x = plot.left()+(t_val-start)/(end-start)*plot.width()
                    if index == 0: path.moveTo(x,y_value(value))
                    else: path.lineTo(x,y_value(value))
                painter.drawPath(path)
            painter.restore()
            if not self.compact:
                painter.setPen(QColor(t.get('scope_text', '#a9bacb')))
                painter.drawText(QRectF(plot.left()+4,baseline-line_height,60,line_height),Qt.AlignmentFlag.AlignLeft,'0 V')
            if y_value(self.voltage) < plot.top():
                painter.setPen(QColor(t.get('scope_warn', '#f2b36f'))); painter.drawText(plot,Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignHCenter,'Amplitude exceeds view')
        painter.setPen(QColor(t.get('scope_text', '#d3dfeb')))
        caption = time_label(self.time_div) + f'   {self.volt_div:g} V/div'
        painter.drawText(QRectF(8,self.height()-line_height-4,self.width()-16,line_height),Qt.AlignmentFlag.AlignCenter,caption)


class ScopePreview(QGroupBox):
    def __init__(self,parent=None):
        super().__init__('Expected Signal',parent)
        layout = QVBoxLayout(self); layout.setContentsMargins(7,5,7,5); layout.setSpacing(3)
        self.trace = ScopeTrace(self,compact=True); layout.addWidget(self.trace,1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Maximum)
        controls = FlowRow()
        self.view_combo = QComboBox(); self.view_combo.addItem('Pulse detail','pulse'); self.view_combo.addItem('Pulse train','train')
        self.view_combo.currentIndexChanged.connect(self.select_view)
        controls.flow.addWidget(self.view_combo)
        self.expand = QPushButton('Adjust View…'); self.expand.clicked.connect(self.open_view)
        controls.flow.addWidget(self.expand)
        self.information = InformationButton('Expected signal',
            'This is an ideal waveform simulation, not a measurement. Output off previews the configured signal. See Oscilloscope Setup in Adjust View for scales, triggering, probe settings, and troubleshooting.')
        controls.flow.addWidget(self.information); layout.addWidget(controls)
        self.dialog = None
        self.custom = False
        self.view = 'pulse'

    def select_view(self):
        self.view = self.view_combo.currentData()
        self.recommend()

    def set_signal(self,voltage,frequency,width_ms,mode='pace'):
        if mode == 'square': frequency,width_ms = 10.,50.
        if mode == 'cycle': frequency /= 8
        signal = (voltage,frequency,width_ms/1000,mode)
        previous = (self.trace.voltage,self.trace.frequency,self.trace.pulse_width_s,self.trace.mode)
        if signal == previous: return
        self.trace.voltage,self.trace.frequency,self.trace.pulse_width_s,self.trace.mode = signal
        if not self.custom: self.recommend()
        self._update()

    def recommend(self):
        self.custom = False
        self.trace.time_div,self.trace.volt_div = recommended_scales(self.trace.voltage,
            self.trace.frequency,self.trace.pulse_width_s,self.trace.mode,self.view)
        self._update()

    def _update(self):
        self.trace.update()
        if self.dialog is not None:
            for name in ('voltage','frequency','pulse_width_s','mode','time_div','volt_div'):
                setattr(self.large_trace,name,getattr(self.trace,name))
            self.large_trace.update()
            self.time_combo.blockSignals(True); self.volt_combo.blockSignals(True)
            self.time_combo.setCurrentIndex(self.time_combo.findData(self.trace.time_div))
            self.volt_combo.setCurrentIndex(self.volt_combo.findData(self.trace.volt_div))
            self.time_combo.blockSignals(False); self.volt_combo.blockSignals(False)
            self.large_view_combo.blockSignals(True)
            self.large_view_combo.setCurrentIndex(self.large_view_combo.findData(self.view))
            self.large_view_combo.blockSignals(False)
            self.trigger.setText('Output isolated · no triggerable signal' if self.trace.mode == 'isolated' else
                ('Auto trigger · DC coupling' if self.trace.mode == 'constant' or self.trace.voltage == 0 else
                 f'Rising-edge trigger · {self.trace.voltage/2:g} V level · DC coupling'))

    def open_view(self):
        if self.dialog is not None:
            self.dialog.show(); self.dialog.raise_(); return
        self.dialog = QDialog(self); self.dialog.setWindowTitle('TEMPO · Expected Oscilloscope Trace'); self.dialog.resize(650,440)
        layout = QVBoxLayout(self.dialog)
        self.large_trace = ScopeTrace(); layout.addWidget(self.large_trace,1)
        form = QFormLayout(); form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        view = QComboBox(); view.addItem('Pulse detail','pulse'); view.addItem('Pulse train','train')
        self.large_view_combo = view
        view.setCurrentIndex(view.findData(self.view))
        def select_view(): self.view_combo.setCurrentIndex(self.view_combo.findData(view.currentData()))
        view.currentIndexChanged.connect(select_view); form.addRow('Recommended view',view)
        self.time_combo = QComboBox(); self.volt_combo = QComboBox()
        for value in TIME_DIVISIONS: self.time_combo.addItem(time_label(value),value)
        for value in VOLT_DIVISIONS: self.volt_combo.addItem(f'{value:g} V/div',value)
        def custom():
            self.custom = True
            self.trace.time_div,self.trace.volt_div = self.time_combo.currentData(),self.volt_combo.currentData()
            self._update()
        self.time_combo.currentIndexChanged.connect(custom); self.volt_combo.currentIndexChanged.connect(custom)
        form.addRow('Time scale',self.time_combo); form.addRow('Voltage scale',self.volt_combo)
        layout.addLayout(form)
        self.trigger = QLabel(); self.trigger.setWordWrap(True); layout.addWidget(self.trigger)
        actions = QHBoxLayout()
        reset = QPushButton('Recommended Scales'); reset.clicked.connect(self.recommend); actions.addWidget(reset)
        helper = QPushButton('Real Scope Setup…'); helper.clicked.connect(self.scope_help); actions.addWidget(helper)
        QShortcut(QKeySequence.StandardKey.Close,self.dialog,activated=self.dialog.close)
        layout.addLayout(actions); self._update(); self.dialog.show()

    def scope_help(self):
        dialog = QDialog(self.dialog); dialog.setWindowTitle('TEMPO · Oscilloscope Setup'); layout = QVBoxLayout(dialog)
        guide = QTextBrowser(); guide.setOpenExternalLinks(False)
        guide.setHtml('''<h2>Viewing stimulation on an oscilloscope</h2>
<p>TEMPO shows an ideal prediction of the commanded output. Its scale controls change only this drawing; they do not configure an oscilloscope or change stimulation.</p>
<h3>1. Set the probe and input</h3>
<p>Match the oscilloscope’s probe attenuation setting to the physical probe (for example, a 10× probe with a 10× channel setting). A mismatch makes the displayed voltage wrong by that factor. Use DC coupling so the baseline and pulse plateau remain visible. Use the input and probe connection specified for your stimulation circuit; check the instrument’s grounding requirements before connecting its ground lead.</p>
<h3>2. Start with the recommended scales</h3>
<p><b>Pulse detail</b> expands one pulse so you can inspect its height and width. <b>Pulse train</b> shows repetition over time. For the default 0.6 V, 1 Hz, 2 ms signal, start at <b>0.2 V/div and 1 ms/div</b> for pulse detail, then <b>0.2 s/div</b> for the train. A period of 1 second corresponds to 1 Hz; 2 ms is the time from the rising edge to the falling edge.</p>
<p>Each horizontal division represents the displayed time/div; each vertical division represents V/div. Increasing time/div shows more time but makes a narrow pulse look thinner. Increasing V/div fits a larger voltage range but makes the pulse shorter on screen.</p>
<h3>3. Stabilize the trigger</h3>
<p>For pulses, select a rising-edge trigger on the channel being viewed, initially at about half the expected pulse height. The default signal therefore suggests roughly 0.3 V. Keep the zero reference below the pulse so both the resting level and plateau fit. For constant output, use Auto trigger to keep the DC level visible; there is no repeating edge to trigger on.</p>
<p>TEMPO fixes its simulated coupling, baseline, and trigger position so these settings cannot accidentally hide the drawing. They remain settings you must check on a physical instrument.</p>
<h3>4. If the trace is difficult to see</h3>
<p><b>Flat-looking train:</b> switch to pulse detail or reduce time/div; a 2 ms pulse occupies only 0.2% of a 1 Hz period. <b>Clipped pulse:</b> increase V/div or restore Recommended Scales. <b>Drifting or missing trace:</b> check trigger source, edge, level, and acquisition mode. <b>Unexpected amplitude:</b> check probe factor, coupling, loading, and the actual circuit connection.</p>
<h3>Diagnostics and interpretation</h3>
<p>The scope-test waveform is 10 Hz with 50% duty cycle. Eight-output cycling delivers one pulse to each output in turn, so each output’s repetition frequency is the requested frequency divided by eight. An isolated output has no predicted voltage in this simulator.</p>
<p>Real traces may show loading, calibration error, rise/fall time, switching transients, or timing differences absent from this ideal drawing. The requested DAC voltage is not a measured electrode voltage. Host timestamps do not establish measured pulse-to-frame synchronization, and device confirmation thresholds are not verified biological safety limits.</p>''')
        layout.addWidget(guide)
        QShortcut(QKeySequence.StandardKey.Close,dialog,activated=dialog.close)
        dialog.resize(620,600); dialog.show(); self._help_dialog = dialog
