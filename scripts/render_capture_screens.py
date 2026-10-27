"""Bounded acquisition layout fixtures, including actual pointer hover.

Run with python -m scripts.render_capture_screens. Images are simulated layout
evidence; this never opens a native window or connects to hardware/services.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


def render(output):
    output.mkdir(parents=True,exist_ok=True)
    config=output/'screen.json'
    config.write_text(json.dumps(dict(screens=[dict(name='Capture review',width=4096,height=2304)])))
    os.environ['QT_QPA_PLATFORM']='offscreen:configfile='+str(config.resolve())
    from PyQt6.QtWidgets import QApplication,QPushButton,QLabel
    from PyQt6.QtCore import Qt,QPoint,QRect
    from PyQt6.QtGui import QFont,QImage,QPainter,QColor,QPixmap
    from PyQt6.QtTest import QTest
    from tempo_ui import install_ui,help_controller,prepare_offscreen_fonts
    from tempo_tasks import dispose_closed_views,dispose_application
    from tempo_config import DEFAULT_PREFERENCES
    from tempo_capture import CaptureSession
    from tempo_stimulation import StimulusParameters,PacerStatus
    app=QApplication.instance() or QApplication([])
    install_ui(app); prepare_offscreen_fonts(app)
    with patch('platform.machine',return_value='x86_64'): import tempo_gui as gui
    records=[]
    def settle():
        for _ in range(12): app.processEvents()
    with tempfile.TemporaryDirectory(prefix='tempo-capture-review-') as directory,\
         patch('tempo_capture_ui.CaptureTools.showEvent',lambda self,event:None),\
         patch.object(gui.tempo_preferences,'_preferences',copy.deepcopy(DEFAULT_PREFERENCES)):
        cases = (
            ('selected-c',(1280,820),12,True,8),('empty',(1100,760),12,False,0),('wide',(1500,950),12,True,8),
            ('small',(960,620),12,True,8),('large-font',(1100,760),18,True,8),
            ('long-history',(1100,760),12,True,150),('pre-manual',(1100,760),12,True,0),
            ('pre-timed',(1100,760),12,True,0),('recording',(1100,760),12,True,1),
            ('output-fault',(1100,760),12,True,1))
        for name,size,font,active,volume in cases:
            app.setFont(QFont('Arial',font))
            view=gui.CaptureTab(); tools=view.capture_controls
            view.resize(*size); view.show(); settle()
            session=None
            if active:
                # Render a connected camera and populated property editor,
                # without starting a feed or calling a driver.
                view.core=SimpleNamespace()
                view.spin_exposure.setEnabled(True); view.btn_cam.setEnabled(True)
                view.btn_cam.setText('Stop Hardware Feed')
                view.prop_combo.blockSignals(True)
                view.prop_combo.addItems(['Binning','Gain','PixelType','SensorReadoutMode','TriggerSource'])
                view.prop_combo.blockSignals(False)
                view.prop_combo.setEnabled(True); view.prop_val_input.setEnabled(True)
                view.prop_val_input.setText('1'); view.btn_apply_prop.setEnabled(True); view.prop_group.show()
                params=StimulusParameters(); status=PacerStatus('PACING',params,False,100,2000,4.77)
                session=CaptureSession(directory,'Review','A01',params,channel=0)
                session.start(status)
                if not name.startswith('pre-'): session.begin_imaging()
                if name=='pre-timed': tools.pre_mode.setCurrentIndex(1); tools.pre_duration.setValue(60)
                for i in range(volume):
                    session.document['fields'].append(dict(field_id=f'FOV{i+1:03d}',path=None,
                        status='completed',counts=dict(live_count=8,dead_count=2,healthy_groups=1,dead_groups=0),
                        credited_cells=8,duplicate=False))
                session.document['healthy_cell_estimate']=volume*8
                tools.session=session
                tools.service=SimpleNamespace(connected=True,running=True,status=status,error='')
                tools._stage_present=True; tools.enabled.setChecked(True)
                tools.show_fields(session.document)
                if name=='selected-c':
                    rest=PacerStatus('REST_DISCHARGE',params,False,100,2000,4.77)
                    session.finish(rest,'Previous recording completed.')
                    tools.service.status=rest; tools.refresh_controls()
                if session.document['fields']: tools._show_counts(session.document['fields'][-1]['counts'])
                if name=='recording':
                    view.is_recording=True; view.acq_worker=SimpleNamespace(isRunning=lambda:True,is_running=True)
                    view.btn_record.setText('Stop Recording')
                if name=='output-fault':
                    tools.service.connected=False; tools.service.status=None
                    tools.error.set_error('Serial connection lost. Output is unconfirmed.',RuntimeError('Simulated disconnect'))
                image=QImage(800,600,QImage.Format.Format_RGB32); image.fill(QColor('#30383d'))
                painter=QPainter(image); painter.setBrush(QColor('#9aabb0')); painter.setPen(QColor('#c6d0d5'))
                for x,y,w,h in ((110,120,90,40),(350,280,120,60),(580,110,85,50),(190,440,100,55),(560,450,110,60)):
                    painter.drawEllipse(x,y,w,h)
                painter.setPen(QColor('#edf2f7')); painter.drawText(20,30,'Simulated field · layout fixture'); painter.end()
                view.feed_label.setPixmap(QPixmap.fromImage(image).scaled(view.feed_label.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                view.btn_record.setEnabled(True); view.btn_snap.setEnabled(True)
            settle()
            if active:
                view.feed_label.setPixmap(QPixmap.fromImage(image).scaled(view.feed_label.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
            tools.history.scrollToBottom()
            settle()
            view.grab().save(str(output/(name+'.png')))
            stop=tools.stop_button
            bounds=QRect(stop.mapTo(view,QPoint()),stop.size())
            assert view.rect().contains(bounds),f'{name}: stop control outside window'
            record_bounds=QRect(view.btn_record.mapTo(view,QPoint()),view.btn_record.size())
            assert view.rect().contains(record_bounds),f'{name}: recording control outside window'
            assert view.camera_panel.isAncestorOf(view.btn_record)
            assert view.stimulation_panel.isAncestorOf(tools.connection_group)
            assert view.size().width()==size[0],f'{name}: window forced wider'
            # Every information icon fits its actual parent layout.
            for icon in (tools.information,tools.count_info,tools.scope_preview.information):
                assert icon.parentWidget().rect().contains(icon.geometry()),f'{name}: clipped information icon'
            for button in (view.btn_connection_settings,view.btn_flir_preset,view.btn_record,tools.setup_button,stop):
                assert button.width()>=button.fontMetrics().horizontalAdvance(button.text())+10,f'{name}: clipped button text'
            for control in (tools.amplitude,tools.frequency,tools.width):
                text_width=control.fontMetrics().horizontalAdvance(control.text())
                assert control.lineEdit().contentsRect().width()>=text_width,f'{name}: clipped stimulation value {control.text()}'
            for label in (tools.off_label,tools.on_label):
                assert label.width()>=label.fontMetrics().horizontalAdvance(label.text()),f'{name}: clipped switch label'
            record=dict(name=name,size=list(size),font=font,panels=view.capture_splitter.sizes(),
                inspector_scroll= view.capture_inspector.verticalScrollBar().maximum(),
                camera_scroll=view.acquisition_scroll.verticalScrollBar().maximum(),
                history_scroll=tools.history.verticalScrollBar().maximum(),stop_visible=True,record_visible=True)
            if name=='wide':
                icon=tools.scope_preview.information
                QTest.mouseMove(view,QPoint(2,2)); QTest.qWait(50)
                QTest.mouseMove(icon,icon.rect().center()); QTest.qWait(220)
                controller=help_controller()
                assert controller.source is icon and controller.bubble is not None and controller.bubble.isVisible()
                view.grab().save(str(output/'scope-hover.png'))
                bubble=controller.bubble
                QTest.mouseMove(bubble,bubble.rect().center()); QTest.qWait(220)
                assert controller.bubble is bubble,'Moving into help dismissed its content'
                QTest.mouseMove(view,QPoint(2,2)); QTest.qWait(220)
                assert controller.bubble is None
                QTest.mouseClick(icon,Qt.MouseButton.LeftButton); settle()
                assert controller.bubble is not None,'Click did not open information'
                QTest.keyClick(icon,Qt.Key.Key_Escape); settle()
                assert controller.bubble is None
                tools.scope_preview.open_view(); settle()
                tools.scope_preview.dialog.grab().save(str(output/'scope-pulse.png'))
                tools.scope_preview.view='train'; tools.scope_preview.recommend(); settle()
                tools.scope_preview.dialog.grab().save(str(output/'scope-train.png'))
                tools.scope_preview.set_signal(.8,2,5,'constant'); settle()
                tools.scope_preview.dialog.grab().save(str(output/'scope-constant.png'))
                tools.scope_preview.scope_help(); settle()
                tools.scope_preview._help_dialog.grab().save(str(output/'scope-guide.png'))
                tools.scope_preview._help_dialog.close()
                tools.scope_preview.dialog.close()
                tools.stimulation_help(); settle()
                tools._guide_dialog.grab().save(str(output/'stimulation-guide.png')); tools._guide_dialog.close()
                view.camera_settings_dialog.show(); settle()
                view.camera_settings_dialog.grab().save(str(output/'camera-settings.png')); view.camera_settings_dialog.close()
                tools.show_history(); settle()
                tools._history_dialog.grab().save(str(output/'recording-history.png')); tools._history_dialog.close()
                session.finish(PacerStatus('REST_DISCHARGE',params,False,100,2000,4.77),'Review fixture complete.')
                tools.refresh_controls()
                tools.open_automatic(); settle()
                tools.automatic_dialog.grab().save(str(output/'automatic-setup.png'))
                tools.automatic_dialog.close()
            records.append(record)
            tools.service=None
            view.is_recording=False; view.acq_worker=None; view.core=None
            if session: session.finish(PacerStatus('REST_DISCHARGE',StimulusParameters(),False,100,2000,4.77),'Review fixture complete.')
            tools.poll_timer.stop(); view.close(); settle(); dispose_closed_views(view)
    (output/'geometry.json').write_text(json.dumps(records,indent=2)+'\n')
    dispose_application(app)
    return records


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('.agent-artifacts/stimulation-review/screens'))
    args=parser.parse_args()
    print(json.dumps(render(args.output),indent=2))
