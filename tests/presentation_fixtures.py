"""Synthetic presentation data; never write to the user's queue or research files."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import numpy as np


def application_style_name(app):
    """Remember the base style beneath TEMPO's focus-neutral proxy."""
    from PyQt6 import sip
    proxy = getattr(app, '_tempo_style', None)
    if proxy is not None and not sip.isdeleted(proxy):
        return proxy.baseStyle().objectName()
    return app.style().objectName() or 'Fusion'


def set_application_style(app, name):
    """Exercise a platform style without discarding TEMPO's focus policy."""
    from tempo_ui import FocusNeutralStyle
    proxy = FocusNeutralStyle(name or 'Fusion')
    app.setStyle(proxy)
    app._tempo_style = proxy


def write_historical_dataset(directory):
    """Old beat-level schema, including repeated cell IDs and no normalized amplitude."""
    rows = []
    random = np.random.default_rng(7)
    for field in range(1, 6):
        for cell in range(1, 17):
            x, y = random.integers([100, 50], [1850, 980])
            for beat in range(1, 4):
                rows.append(dict(Well_ID='A01', FOV_ID=field, Cell_ID=f'cell_{cell:03}',
                    Peak_Number=beat, Centroid_X=x, Centroid_Y=y, Peak_Amplitude_au=40+cell+beat,
                    Time_to_Peak_ms=160+cell, Relaxation_Time_ms=330+cell,
                    CT50_ms=75+cell, CT90_ms=160+cell, RT50_ms=175+cell, RT90_ms=330+cell,
                    Max_Contraction_Velocity=1.5, Max_Relaxation_Velocity=.7,
                    Peak_to_Peak_Time_ms=1600+cell))
    path = Path(directory)/'Historical_Schema_Results.csv'
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def populated_review(directory):
    """Local image review only: intercept every remote read and construct its responses."""
    import annotation_review_dialog as review
    from PyQt6.QtGui import QImage, QPainter, QColor
    path = Path(directory)/'review-image.png'
    image = QImage(640, 480, QImage.Format.Format_RGB32); image.fill(QColor('#303944'))
    painter = QPainter(image)
    painter.setBrush(QColor('#758996'))
    for x, y in ((120,100), (340,180), (460,350)):
        painter.drawEllipse(x, y, 100, 60)
    painter.end(); image.save(str(path))
    record = dict(width=640, height=480, coordinate_space='pixels', boxes=[
        dict(class_id=i % 2, polygon=[[x,y],[x+100,y],[x+100,y+60],[x,y+60]])
        for i,(x,y) in enumerate(((120,100),(340,180),(460,350)))])
    pending=[]
    with patch.object(review, 'run_background', side_effect=lambda action,done: pending.append(done)):
        view = review.AnnotationReviewDialog(verified=True)
        pending.pop(0)([dict(submission_id='synthetic',collaborator_id='Preview',image_count=1)],None)
        view.sub_list.setCurrentRow(0)
        pending.pop(0)({'review-image.png':copy.deepcopy(record)},None)
        pending.pop(0)(str(path),None)
    # This is a fixed presentation snapshot. Showing it later must not start a
    # live account read after the construction-time transport patch has ended.
    # Refresh ordering is exercised separately by ReviewInteractionTests.
    view.request_refresh = lambda: None
    return view


def write_dataset(directory, registered=False, traces=False):
    directory = Path(directory)
    rows = []
    for field_index, field in enumerate(('FOV001', 'FOV002')):
        recording = 'recording-' + str(field_index + 1)
        for cell in range(1, 7):
            rows.append(dict(Recording_ID=recording, Well_ID='A01', FOV_ID=field,
                Cell_ID=str(cell), Centroid_X=cell*8, Centroid_Y=10+cell*4,
                Peak_Amplitude_au=40+cell, Peak_Amplitude_AreaNorm_au=(40+cell)/100, Time_to_Peak_ms=160+cell,
                Relaxation_Time_ms=330+cell, Peak_to_Peak_Time_ms=1600+cell,
                CT50_ms=75+cell, CT90_ms=160+cell, RT50_ms=175+cell, RT90_ms=330+cell,
                Max_Contraction_Velocity=1.5, Max_Relaxation_Velocity=.7,
                Fractional_Anisotropy=.6, Contraction_Angle_deg=179 if cell%2 else 1,
                Vector_Coherence=.8, Total_Beats_Analyzed=8,
                Spatial_Metadata_File=f'{recording}.spatial.json' if registered else '',
                Trace_File=f'{recording}-cell-{cell}.csv' if traces else ''))
            if traces:
                pd.DataFrame({'Time (ms)': [0,50,100,150,200,250,300],
                    'Contraction (Raw a.u.)': [0,5,20,40+cell,20,5,0],
                    'Contraction (Area-Norm a.u.)': [0,.05,.2,(40+cell)/100,.2,.05,0],
                    'Contraction (Fractional dI/I0)': [0,.025,.1,(40+cell)/200,.1,.025,0],
                    'Speed (a.u.)': [0,1,2,1,2,1,0]}).to_csv(directory/rows[-1]['Trace_File'], index=False)
        if registered:
            document = dict(version=1, recording_id=recording, well_id='A01', field_id=field,
                units='um', coordinate_system='A01-centred', image_size_px=[64,48],
                pixel_to_well=[[3,0,field_index*240-200],[0,3,-80]],
                well_geometry=dict(center_x=0, center_y=0, radius=500))
            (directory/f'{recording}.spatial.json').write_text(json.dumps(document))
    path = directory/'Synthetic_Results.csv'
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def processing_view(gui):
    from PyQt6.QtWidgets import QMainWindow, QWidget
    class ProcessingFixture(gui.TempoDashboard):
        def __init__(self):
            QMainWindow.__init__(self)
            self.tab_cruncher = QWidget()
            self.setCentralWidget(self.tab_cruncher)
            self.setup_cruncher_tab()
        def closeEvent(self, event):
            # This fixture constructs only Processing: no hardware, tasks or workers.
            event.accept()
    return ProcessingFixture()
