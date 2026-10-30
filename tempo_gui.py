import sys
import os
import platform

if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] in ('--assist-model', '--assist-model-cuda'):
    if len(sys.argv) != 8:
        sys.exit(2)
    from tempo_assistance_runtime import assist_command
    sys.exit(assist_command(*sys.argv[2:], require_cuda=sys.argv[1] == '--assist-model-cuda'))

if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--probe-cuda':
    if len(sys.argv) != 3:
        sys.exit(2)
    from tempo_nvidia_runtime import probe_command
    sys.exit(probe_command(sys.argv[2]))

if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--predict-cells':
    if len(sys.argv) != 7:
        sys.exit(2)
    from tempo_nvidia_runtime import predict_command
    sys.exit(predict_command(*sys.argv[2:]))

if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == '--predict-nuclei':
    if len(sys.argv) != 5:
        sys.exit(2)
    from tempo_nuclear_training import prediction_command
    sys.exit(prediction_command(*sys.argv[2:]))

if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--verify-installation':
    from tempo_release import verify_installation
    sys.exit(verify_installation(sys.argv[2]))

if __name__ == '__main__' and len(sys.argv) == 4 and sys.argv[1] == '--verify-probe':
    from tempo_release import verification_probe
    sys.exit(verification_probe(sys.argv[2], sys.argv[3]))

# --- MACOS ARCHITECTURE FIX ---
# Micro-Manager hardware drivers (like Leica, Nikon) are only built for Intel (x86_64).
# If this script is launched natively on an M-series Mac (arm64), we seamlessly 
# restart the entire python process under Rosetta 2 before loading any heavy libraries.
if platform.system() == "Darwin" and platform.machine() == "arm64":
    venv_x86_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv_x86", "bin", "python")
    if os.path.exists(venv_x86_python):
        os.execvp("arch", ["arch", "-x86_64", venv_x86_python] + sys.argv)
    else:
        print("[ERROR] .venv_x86 environment not found. GUI requires x86_64 packages.")
        sys.exit(1)

if __name__ == '__main__':
    from tempo_startup_diagnostics import start_from_arguments, record as startup_stage
    start_from_arguments(sys.argv)
    startup_stage('runtime.started')
    from tempo_runtime import prepare_windows_runtime
    prepare_windows_runtime()
    startup_stage('runtime.completed')

import cv2
import contextlib
import copy
import importlib
import shutil
import random
import json
import subprocess
import platform
from pathlib import Path
import time
import re
import tempfile
import threading
from PyQt6.QtWidgets import (QApplication, QMainWindow, QPushButton, QVBoxLayout,
                             QWidget, QTextEdit, QFileDialog, QLabel, QDialog, QMenu,
                             QHBoxLayout, QRubberBand, QTabWidget, QTabBar, QSplitter,
                             QGridLayout, QComboBox, QScrollArea, QGroupBox, QFormLayout,
                             QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QListWidget, QListWidgetItem,
                             QMessageBox, QGraphicsView, QGraphicsScene, QGraphicsRectItem, QAbstractItemView,
                             QStackedWidget, QProgressBar, QSlider, QTableWidget, QTableWidgetItem,
                             QHeaderView, QSizePolicy, QLayout, QFrame, QGraphicsPolygonItem, QGraphicsTextItem, QGraphicsPixmapItem)
from PyQt6.QtGui import QDesktopServices, QImage, QPixmap, QPen, QColor, QPainter, QPainterPath, QPainterPathStroker, QIcon, QBrush, QPolygonF, QFont, QFontMetrics, QShortcut, QKeySequence
from PyQt6.QtCore import QUrl, QThread, pyqtSignal, Qt, QProcess, QRect, QPoint, QPointF, QTimer, QRectF, QItemSelectionModel, QEvent, QSignalBlocker

# Generated caches belong to the user profile, including in frozen installs.
from tempo_runtime import configure_matplotlib_cache
mpl_cache_dir = configure_matplotlib_cache()

import matplotlib
matplotlib.use('QtAgg')
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import pandas as pd
import numpy as np
import pyqtgraph as pg
import tempo_backend
import tempo_statistics
from tempo_angles import axial_statistics
from tempo_analysis import read_results, summarize_cells, read_trace
from tempo_spatial import registration_for_row, transform_points, field_outline
from tempo_processing import CancellationToken
from tempo_config import tempo_preferences
from tempo_credentials import safe_profile_name, redact_credentials
from tempo_secure_store import CredentialMigrationRequired
import datetime
from yolo_inference_helper import detect_cells_via_bridge
from tempo_hub import tempo_hub, atomic_json, expired, configure_desktop_http
configure_desktop_http()
from tempo_app_icon import application_icon_path
from tempo_annotation_store import update_session, atomic_bytes, AnnotationSessionConflict
from tempo_image_browser import SharedImageBrowser
from tempo_annotations import (normalize_record, validate_annotations, pixel_record,
                               merge_image_records, canonical_image_name)
from tempo_regions import object_type, region_counts, scout_qualifies
from tempo_tasks import run_read_task, run_task, run_background, TaskThread, pending_task_titles
from tempo_ui import confirm_action, FilenameLabel, ErrorDetails, InformationButton, InformationSlot, FilePickerRow, set_button_color, FlowLayout, FlowRow, SettingRow, MetricForm, StatisticsTabs, statistic_description, InlineNotice, ContentTabs, CompactWorkspace, SelectorRow, install_help, ContainedToolsMenu, ACTION_GREEN, DESCRIPTION_BLUE, GUIDANCE_MINT
from tempo_ui import PreviewStatusLabel
from tempo_plot import LifecycleAxisItem, PlotWidget, ColorBarItem, add_plot, add_item

try:
    from pymmcore_plus import CMMCorePlus # type: ignore
    HAS_MMCORE = True
except Exception as e:
    print(f"[WARNING] Micro-Manager hardware disabled. Reason: {e}")
    HAS_MMCORE = False

def get_configured_core():
    """
    Returns a CMMCorePlus instance. This function has been reset to its simplest
    form to rely on the library's default, stable auto-discovery of its own
    downloaded binaries, removing all prior complex workarounds.
    """
    if not HAS_MMCORE:
        return None
    core = CMMCorePlus.instance()
    adapter_path = tempo_preferences.get('capture', 'mm_adapter_path', '')
    if adapter_path:
        core.setDeviceAdapterSearchPaths([adapter_path])
    return core

# --- 1. The Native PyQt ROI Selector ---
class ROIDialog(QDialog):
    """A native PyQt popup to draw a bounding box, completely bypassing OpenCV's GUI."""
    def __init__(self, video_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select a Cell Region")
        self.video_path = video_path
        self.roi_coords = None

        self.layout = QVBoxLayout(self)

        self.info_label = QLabel("Click and drag to select the cell, then click Confirm.")
        self.layout.addWidget(self.info_label)

        self.image_label = QLabel()
        self.image_label.setCursor(Qt.CursorShape.CrossCursor)
        self.layout.addWidget(self.image_label)

        # Optional Metadata Input
        self.metadata_group = QGroupBox("Optional Spatial Metadata (Used for Analyzer)")
        m_layout = QFormLayout(self.metadata_group)
        self.well_input = QLineEdit()
        self.well_input.setPlaceholderText("e.g. A04")
        self.cx_input = QLineEdit()
        self.cx_input.setPlaceholderText("e.g. 256")
        self.cy_input = QLineEdit()
        self.cy_input.setPlaceholderText("e.g. 256")
        m_layout.addRow("Well ID:", self.well_input)
        m_layout.addRow("Centroid X:", self.cx_input)
        m_layout.addRow("Centroid Y:", self.cy_input)
        self.layout.addWidget(self.metadata_group)

        btn_layout = QHBoxLayout()
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.clicked.connect(self.accept)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addWidget(self.btn_confirm)
        self.layout.addLayout(btn_layout)

        # The rubber band is the visual box you draw
        self.rubber_band = QRubberBand(QRubberBand.Shape.Rectangle, self.image_label)
        self.origin = QPoint()

        self.load_first_frame()

    def load_first_frame(self):
        """Extracts the first frame using headless OpenCV and loads it into the Qt Label."""
        from tempo_recording import open_recording,display_frame
        cap = open_recording(self.video_path)
        ret, frame = cap.read()
        cap.release()
        
        if ret:
            frame = display_frame(frame)
            if frame.ndim==2:
                frame=cv2.cvtColor(frame,cv2.COLOR_GRAY2BGR)
            # Convert OpenCV BGR to Qt RGB
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            
            # Load into Qt
            qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            self.pixmap = QPixmap.fromImage(qimg)
            self.image_label.setPixmap(self.pixmap)
            
            # Attach native mouse events
            self.image_label.mousePressEvent = self.mouse_press
            self.image_label.mouseMoveEvent = self.mouse_move
            self.image_label.mouseReleaseEvent = self.mouse_release

    def mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.origin = event.pos()
            self.rubber_band.setGeometry(QRect(self.origin, self.origin))
            self.rubber_band.show()

    def mouse_move(self, event):
        if not self.origin.isNull():
            self.rubber_band.setGeometry(QRect(self.origin, event.pos()).normalized())

    def mouse_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            rect = self.rubber_band.geometry()
            self.roi_coords = (rect.x(), rect.y(), rect.width(), rect.height())

    def get_cells_data(self):
        """Formats the coordinates for the backend masking engine."""
        if self.roi_coords:
            x, y, w, h = self.roi_coords
            if w == 0 or h == 0: 
                return None
            return [{
                "id": "manual_cell_1", 
                "polygon": [[x, y], [x+w, y], [x+w, y+h], [x, y+h]],
                "well_id": self.well_input.text().strip(),
                "centroid_x": self.cx_input.text().strip(),
                "centroid_y": self.cy_input.text().strip()
            }]
        return None

def get_video_frame_0(video_path):
    """Robustly extracts frame 0 of a video or loads a static image."""
    if not os.path.exists(video_path):
        return None
    ext = os.path.splitext(video_path)[1].lower()
    if ext in ('.tif','.tiff'):
        from tempo_recording import open_recording,display_frame,read_metadata
        cap=open_recording(video_path)
        try:
            ok,frame=cap.read()
            if not ok:return None
            frame=display_frame(frame,read_metadata(video_path).get('sensor_bits'))
            return cv2.cvtColor(frame,cv2.COLOR_GRAY2BGR) if frame.ndim==2 else np.ascontiguousarray(frame[:,:,:3])
        finally:cap.release()
    if ext in ['.tif', '.tiff', '.png', '.jpg', '.jpeg', '.bmp']:
        return cv2.imread(video_path)
    
    from tempo_recording import open_recording
    cap = open_recording(video_path)
    ret, frame = cap.read()
    cap.release()
    if ret and frame is not None:
        return frame
        
    # Subprocess fallback for macOS Sequoia / Rosetta codec limitations
    base_dir = os.path.dirname(os.path.abspath(__file__))
    arm64_py = os.path.join(base_dir, ".venv", "bin", "python")
    temp_jpg = os.path.join(tempfile.gettempdir(), f"frame0_{os.getpid()}_{random.randint(1000, 9999)}.jpg")
    cmd = ["arch", "-arm64", arm64_py, "-c",
           f"import cv2; cap = cv2.VideoCapture('{video_path}'); ret, frame = cap.read(); cv2.imwrite('{temp_jpg}', frame) if ret else None"]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        if os.path.exists(temp_jpg):
            f = cv2.imread(temp_jpg)
            try: os.remove(temp_jpg)
            except OSError: pass
            return f
    except Exception:
        pass
    return None

class InteractiveCellPolygonItem(QGraphicsPolygonItem):
    """Interactive polygon in the cell review scene allowing click-to-toggle inclusion."""
    def __init__(self, cell_idx, cell_dict, on_toggle_callback=None):
        poly = QPolygonF([QPointF(pt[0], pt[1]) for pt in cell_dict.get("polygon", [])])
        super().__init__(poly)
        self.cell_idx = cell_idx
        self.cell_dict = cell_dict
        self.on_toggle_callback = on_toggle_callback
        self.is_active = True
        self.setAcceptHoverEvents(True)
        
        # Center label child item
        poly_pts = cell_dict.get("polygon", [])
        if poly_pts:
            cx = float(np.mean([pt[0] for pt in poly_pts]))
            cy = float(np.mean([pt[1] for pt in poly_pts]))
        else:
            cx, cy = 0.0, 0.0
        self.text_item = QGraphicsTextItem(f"{'G' if object_type(cell_dict) == 'group' else 'C'}{cell_idx+1}", self)
        self.text_item.setDefaultTextColor(QColor(255, 255, 255))
        font = QFont("Arial", 9, QFont.Weight.Bold)
        self.text_item.setFont(font)
        self.text_item.setPos(cx - 10, cy - 10)
        
        self.update_style()

    def update_style(self, hovered=False):
        cls_id = self.cell_dict.get("class_id", 0)
        if not self.is_active:
            pen = QPen(QColor(140, 140, 140, 160), 1, Qt.PenStyle.DashLine)
            brush = QBrush(QColor(80, 80, 80, 40))
            self.text_item.setDefaultTextColor(QColor(160, 160, 160, 160))
        elif cls_id == 0:  # Healthy / Live (Green)
            alpha = 100 if hovered else 55
            pen = QPen(QColor(46, 204, 113, 255), 3 if hovered else 2)
            brush = QBrush(QColor(46, 204, 113, alpha))
            self.text_item.setDefaultTextColor(QColor(255, 255, 255, 255))
        else:  # Dead / Pyknotic (Red)
            alpha = 100 if hovered else 55
            pen = QPen(QColor(231, 76, 60, 255), 3 if hovered else 2)
            brush = QBrush(QColor(231, 76, 60, alpha))
            self.text_item.setDefaultTextColor(QColor(255, 255, 255, 255))
        self.setPen(pen)
        self.setBrush(brush)

    def set_active(self, active):
        self.is_active = active
        self.update_style()

    def hoverEnterEvent(self, event):
        self.update_style(hovered=True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.update_style(hovered=False)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_active = not self.is_active
            self.update_style()
            if self.on_toggle_callback:
                self.on_toggle_callback(self.cell_idx, self.is_active)
        super().mousePressEvent(event)

class AutoCellReviewDialog(QDialog):
    """Cellpose review and inline outline correction on a source reference frame."""
    def __init__(self, video_path, parent=None, cells_data=None):
        super().__init__(parent)
        self.video_path = video_path
        self.detected_cells = []
        self.polygon_items = []
        self.frame_img = None
        self.is_updating_ui = False
        self._geometry_edited = False
        self._replace_id = None
        self._has_populated = False
        self._outline_undo = []
        self.analysis_revision=cells_data is not None
        
        self.setWindowTitle("Review Detected Cells")
        self.resize(1200, 800)
        
        self.setup_ui()
        QShortcut(QKeySequence.StandardKey.Undo,self,activated=self.undo_outline_shortcut)
        if cells_data is None:
            self.load_frame_and_run_yolo(conf=0.15)
        else:
            self.frame_img=get_video_frame_0(video_path)
            self.detected_cells=copy.deepcopy(cells_data)
            self.populate_cells()

    def setup_ui(self):
        main_layout = QHBoxLayout(self)
        
        # --- LEFT: Interactive Scene & Viewer ---
        left_box = QVBoxLayout()
        
        # Viewer Toolbar
        view_toolbar = QHBoxLayout()
        self.lbl_view_info = FilenameLabel()
        self.lbl_view_info.setText('Reference frame: '+os.path.basename(self.video_path))
        self.lbl_view_info.setToolTip(os.path.basename(self.video_path))
        self.lbl_view_info.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        view_toolbar.addWidget(self.lbl_view_info,1)
        view_toolbar.addStretch()
        
        btn_zoom_in = QPushButton("+ Zoom In")
        btn_zoom_in.clicked.connect(lambda: self.view.scale(1.25, 1.25))
        btn_zoom_out = QPushButton("- Zoom Out")
        btn_zoom_out.clicked.connect(lambda: self.view.scale(0.8, 0.8))
        btn_fit = QPushButton("Fit in View")
        btn_fit.clicked.connect(self.fit_view)
        
        for b in [btn_zoom_in, btn_zoom_out, btn_fit]:
            view_toolbar.addWidget(b)
        left_box.addLayout(view_toolbar)
        
        self.view = AnnotationView()
        self.scene = self.view.scene
        self.view.set_draw_mode('none')
        self.view.polygon_drawn.connect(self.outline_drawn)
        self.view.mask_selected.connect(lambda index,additive:self.table.selectRow(index))
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing)
        left_box.addWidget(self.view, stretch=1)
        
        main_layout.addLayout(left_box, stretch=3)
        
        # --- RIGHT: Review & Metadata Sidebar ---
        right_panel = QWidget()
        right_panel.setFixedWidth(max(400,self.fontMetrics().horizontalAdvance('Process Selected Cells (100 Selected)')+36))
        side=QVBoxLayout(right_panel);side.setContentsMargins(0,0,0,0)
        self.review_scroll=QScrollArea();self.review_scroll.setWidgetResizable(True)
        self.review_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.review_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content=QWidget();right_layout = QVBoxLayout(content)
        right_layout.setContentsMargins(10, 0, 10, 0)
        self.review_scroll.setWidget(content);side.addWidget(self.review_scroll,1)
        
        # Detection Summary
        summary_group = QGroupBox("Detected Cells")
        summary_layout = QVBoxLayout(summary_group)
        self.lbl_status = QLabel("Loading cell outlines…")
        self.lbl_status.setWordWrap(True)
        self.lbl_counts = QLabel("Detected: 0 cells (0 Live, 0 Dead)")
        self.lbl_counts.setWordWrap(True)
        summary_layout.addWidget(self.lbl_status)
        summary_layout.addWidget(self.lbl_counts)
        right_layout.addWidget(summary_group)

        corrections=QGroupBox('Correct outlines');edits=QVBoxLayout(corrections)
        self.outline_mode=QComboBox()
        for text,mode in (('Inspect','inspect'),('Add outline','add'),('Replace selected outline','replace')):
            self.outline_mode.addItem(text,mode)
        self.outline_mode.currentIndexChanged.connect(self.outline_mode_changed)
        edits.addWidget(self.outline_mode)
        self.outline_class=QComboBox()
        for text,value in (('Healthy cell',(0,'cell')),('Dead cell',(1,'cell')),
                           ('Healthy group',(0,'group')),('Dead group',(1,'group'))):
            self.outline_class.addItem(text,value)
        edits.addWidget(self.outline_class)
        self.label_outline_button=QPushButton('Label Selected');self.label_outline_button.clicked.connect(self.label_outline)
        self.delete_outline_button=QPushButton('Delete Selected');self.delete_outline_button.clicked.connect(self.delete_outline)
        self.label_outline_button.setEnabled(False);self.delete_outline_button.setEnabled(False)
        edits.addWidget(self.label_outline_button);edits.addWidget(self.delete_outline_button)
        self.drawing_note=QLabel();self.drawing_note.setWordWrap(True);self.drawing_note.hide();edits.addWidget(self.drawing_note)
        self.view.drawing_status.connect(self.drawing_status)
        right_layout.addWidget(corrections)
        
        # Plate & FOV Metadata
        meta_group = QGroupBox("Spatial Metadata")
        meta_layout = QFormLayout(meta_group)
        
        # Auto-parse Well and FOV from filename
        fn = os.path.basename(self.video_path)
        well_match = re.search(r'_([A-H]\d{2})_', fn, re.IGNORECASE)
        fov_match = re.search(r'_(FOV\d+)_', fn, re.IGNORECASE)
        
        def_well = well_match.group(1).upper() if well_match else "A01"
        def_fov = fov_match.group(1).upper() if fov_match else "FOV001"
        
        self.well_input = QLineEdit(def_well)
        self.fov_input = QLineEdit(def_fov)
        meta_layout.addRow("Well ID:", self.well_input)
        meta_layout.addRow("FOV ID:", self.fov_input)
        right_layout.addWidget(meta_group)
        
        # AI Model Sensitivity
        sens_group = QGroupBox("Cell Outlines")
        sens_layout = QVBoxLayout(sens_group)
        
        slider_row = QHBoxLayout()
        self.slider_conf = QSlider(Qt.Orientation.Horizontal)
        self.slider_conf.setRange(5, 50) # 0.05 to 0.50
        self.slider_conf.setValue(15)     # default 0.15
        self.lbl_conf_val = QLabel("Conf: 0.15")
        self.lbl_conf_val.setMinimumWidth(self.lbl_conf_val.fontMetrics().horizontalAdvance('Conf: 0.50')+8)
        self.slider_conf.valueChanged.connect(lambda v: self.lbl_conf_val.setText(f"Conf: {v/100:.2f}"))
        slider_row.addWidget(self.slider_conf)
        slider_row.addWidget(self.lbl_conf_val)
        sens_layout.addLayout(slider_row)
        
        self.btn_re_detect = QPushButton("Re-Run AI Detection")
        set_button_color(self.btn_re_detect, '#2D9CDB')
        self.btn_re_detect.clicked.connect(self.rerun_detection)
        sens_layout.addWidget(self.btn_re_detect)
        right_layout.addWidget(sens_group)
        
        # Selection Toggles
        btn_bar = FlowRow()
        self.btn_sel_live = QPushButton("Select Live Only")
        set_button_color(self.btn_sel_live, ACTION_GREEN)
        self.btn_sel_live.clicked.connect(self.select_live_only)
        
        self.btn_sel_all = QPushButton("Select All")
        self.btn_sel_all.clicked.connect(lambda: self.set_all_selected(True))
        
        self.btn_desel_all = QPushButton("Deselect All")
        self.btn_desel_all.clicked.connect(lambda: self.set_all_selected(False))
        
        btn_bar.flow.addWidget(self.btn_sel_live)
        btn_bar.flow.addWidget(self.btn_sel_all)
        btn_bar.flow.addWidget(self.btn_desel_all)
        right_layout.addWidget(btn_bar)
        
        # Cell Detections Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Include", "Cell", "Viability", "Area (px²)"])
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
        for column in (0,2,3):self.table.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemChanged.connect(self.on_table_item_changed)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self.outline_selection_changed)
        self.table.setMinimumHeight(140)
        right_layout.addWidget(self.table, stretch=1)
        
        # Bottom Confirmation
        self.btn_confirm = QPushButton("Process Selected Cells")
        self.btn_confirm.setEnabled(False)
        set_button_color(self.btn_confirm, ACTION_GREEN)
        self.btn_confirm.clicked.connect(self.accept)
        
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        
        side.addWidget(self.btn_confirm)
        side.addWidget(self.btn_cancel)
        
        main_layout.addWidget(right_panel)

    def load_frame_and_run_yolo(self, conf=0.15):
        self.lbl_status.show()
        frame = get_video_frame_0(self.video_path)
        self.frame_img = frame
        if self.frame_img is None:
            self.lbl_status.setText("Unable to read the video reference frame. Choose another video.")
            return
            
        self.lbl_status.setText("Detecting cells…")
        try:
            res = run_task(self,"Outlining cells…",lambda:detect_cells_via_bridge(self.video_path, "cellpose", conf=conf, iou=0.40))
            if not res.get('success'):raise RuntimeError(res.get('error') or 'Cell detection failed.')
            from masking_engine import validate_cells
            candidates=[dict(cell,id=cell.get('id',f'cell_{index+1}')) for index,cell in enumerate(res.get('boxes',[]))]
            validate_cells(candidates)
        except Exception as exc:
            self.lbl_status.setText('Detection failed. Existing outlines were kept. '+str(exc))
            return
        self.detected_cells=copy.deepcopy(candidates)
        self._geometry_edited=False
        self._outline_undo.clear()
        self.populate_cells()

    def populate_cells(self):
        if self.frame_img is None:
            self.lbl_status.setText('Reference image is unavailable.')
            self.btn_confirm.setEnabled(False)
            return
        self.view._cleanup_drawing()
        self.view.set_mask_items([])
        self.scene.clear();self.polygon_items.clear();self.table.setRowCount(0)
        h, w = self.frame_img.shape[:2]
        frame_rgb = cv2.cvtColor(self.frame_img, cv2.COLOR_BGR2RGB)
        bytes_per_line = 3 * w
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        
        self.pixmap_item = QGraphicsPixmapItem(pixmap)
        self.view.image_item=self.pixmap_item
        self.scene.addItem(self.pixmap_item)
        self.scene.setSceneRect(0, 0, w, h)
        
        # Populate interactive scene polygons and table
        self.is_updating_ui = True
        live_count = 0
        dead_count = 0
        
        self.table.setRowCount(len(self.detected_cells))
        for idx, cell in enumerate(self.detected_cells):
            cell.setdefault('id',f'cell_{idx+1}')
            # Compute area
            poly = cell.get("polygon", [])
            area_px = 0
            if len(poly) >= 3:
                pts = np.array(poly, dtype=np.int32)
                area_px = int(cv2.contourArea(pts))
            cell["area_px"] = area_px
            
            cls_id = cell.get("class_id", 0)
            if object_type(cell) == "cell":
                if cls_id == 0: live_count += 1
                else: dead_count += 1
            
            # Interactive Polygon Item
            poly_item = InteractiveCellPolygonItem(idx, cell, on_toggle_callback=self.on_polygon_toggled)
            poly_item.set_active(cell.get('_included',True))
            self.scene.addItem(poly_item)
            self.polygon_items.append(poly_item)
            
            # Table Row
            chk_item = QTableWidgetItem()
            chk_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk_item.setCheckState(Qt.CheckState.Checked if poly_item.is_active else Qt.CheckState.Unchecked)
            
            id_item = QTableWidgetItem(display_cell_name(cell['id'],object_type(cell)))
            id_item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
            
            status_text = "Healthy" if cls_id == 0 else "Dead"
            status_color = QColor(46, 204, 113) if cls_id == 0 else QColor(231, 76, 60)
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(status_color)
            status_item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
            
            area_item = QTableWidgetItem(f"{area_px:,}")
            area_item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
            
            self.table.setItem(idx, 0, chk_item)
            self.table.setItem(idx, 1, id_item)
            self.table.setItem(idx, 2, status_item)
            self.table.setItem(idx, 3, area_item)
            
        self.is_updating_ui = False
        self.view.set_mask_items(self.polygon_items)
        self.outline_selection_changed()
        
        total = len(self.detected_cells)
        self.lbl_status.hide()
        groups = sum(object_type(cell) == "group" for cell in self.detected_cells)
        self.lbl_counts.setText(f"Detected: {total - groups} cells ({live_count} Live, {dead_count} Dead)" + (f" · {groups} groups" if groups else ""))
        self.update_confirm_button()
        if not self._has_populated:
            self.fit_view();self._has_populated=True

    def fit_view(self):
        if self.scene.width() > 0 and self.scene.height() > 0:
            self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def rerun_detection(self):
        if self._geometry_edited and not confirm_action(self,'Replace corrected outlines?',
            'Running detection again replaces the current outline edits. Previous saved analyses remain intact.','Run Detection'):
            return
        conf = self.slider_conf.value() / 100.0
        self.load_frame_and_run_yolo(conf=conf)

    def drawing_status(self,message):
        if not message and self.outline_mode.currentData() in ('add','replace'):
            message='Click outline corners; right-click to finish. Escape cancels the unfinished outline.'
        self.drawing_note.setText(message);self.drawing_note.setVisible(bool(message))

    def outline_mode_changed(self):
        mode=self.outline_mode.currentData()
        self._replace_id=None
        if mode=='replace':
            row=self.table.currentRow()
            if row<0:
                self.outline_mode.setCurrentIndex(0)
                self.drawing_status('Select an outline in the table, then choose Replace selected outline.')
                return
            self._replace_id=self.detected_cells[row]['id']
        self.view.set_draw_mode('none' if mode=='inspect' else 'polygon')
        self.drawing_status('')

    def outline_selection_changed(self):
        if self.is_updating_ui:return
        row=self.table.currentRow()
        self.label_outline_button.setEnabled(row>=0);self.delete_outline_button.setEnabled(row>=0)
        for index,item in enumerate(self.polygon_items):
            item.update_style()
            if index==row:
                pen=item.pen();pen.setWidthF(4);item.setPen(pen)

    def outline_drawn(self,polygon):
        mode=self.outline_mode.currentData()
        if mode not in ('add','replace'):return
        self.remember_inclusion()
        self._outline_undo.append(copy.deepcopy(self.detected_cells))
        if mode=='replace':
            cell=next((cell for cell in self.detected_cells if cell['id']==self._replace_id),None)
            if cell is None:
                self.drawing_status('Select an outline again before replacing it.');return
            cell['polygon']=copy.deepcopy(polygon)
        else:
            used={cell['id'] for cell in self.detected_cells};index=1
            while f'cell_{index}' in used:index+=1
            viability,kind=self.outline_class.currentData()
            self.detected_cells.append(dict(id=f'cell_{index}',class_id=viability,object_type=kind,polygon=copy.deepcopy(polygon)))
        self._geometry_edited=True
        self.outline_mode.setCurrentIndex(0)
        self.populate_cells()

    def remember_inclusion(self):
        for item in self.polygon_items:item.cell_dict['_included']=item.is_active

    def label_outline(self):
        row=self.table.currentRow()
        if row<0:return
        self.remember_inclusion()
        self._outline_undo.append(copy.deepcopy(self.detected_cells))
        viability,kind=self.outline_class.currentData()
        self.detected_cells[row].update(class_id=viability,object_type=kind)
        self._geometry_edited=True;self.outline_mode.setCurrentIndex(0)
        self.populate_cells();self.table.selectRow(row)

    def delete_outline(self):
        row=self.table.currentRow()
        if row<0:return
        self.remember_inclusion();self._outline_undo.append(copy.deepcopy(self.detected_cells));self.detected_cells.pop(row)
        self._geometry_edited=True;self.outline_mode.setCurrentIndex(0);self.populate_cells()

    def undo_outline_shortcut(self):
        focused=QApplication.focusWidget()
        if isinstance(focused,QLineEdit) and self.isAncestorOf(focused):
            focused.undo();return
        self.undo_outline_edit()

    def undo_outline_edit(self):
        if self.view.current_polygon_pts:
            self.view._cleanup_drawing();return
        if self._outline_undo:
            self.detected_cells=self._outline_undo.pop()
            self._geometry_edited=True;self.outline_mode.setCurrentIndex(0);self.populate_cells()

    def on_polygon_toggled(self, cell_idx, is_active):
        if self.is_updating_ui: return
        self.is_updating_ui = True
        item = self.table.item(cell_idx, 0)
        if item:
            item.setCheckState(Qt.CheckState.Checked if is_active else Qt.CheckState.Unchecked)
        self.is_updating_ui = False
        self.table.selectRow(cell_idx)
        self.update_confirm_button()

    def on_table_item_changed(self, item):
        if self.is_updating_ui or item.column() != 0: return
        row = item.row()
        is_checked = (item.checkState() == Qt.CheckState.Checked)
        if 0 <= row < len(self.polygon_items):
            self.polygon_items[row].set_active(is_checked)
        self.update_confirm_button()

    def select_live_only(self):
        self.is_updating_ui = True
        for idx, item in enumerate(self.polygon_items):
            cls_id = item.cell_dict.get("class_id", 0)
            is_live = (cls_id == 0)
            item.set_active(is_live)
            t_item = self.table.item(idx, 0)
            if t_item:
                t_item.setCheckState(Qt.CheckState.Checked if is_live else Qt.CheckState.Unchecked)
        self.is_updating_ui = False
        self.update_confirm_button()

    def set_all_selected(self, selected):
        self.is_updating_ui = True
        for idx, item in enumerate(self.polygon_items):
            item.set_active(selected)
            t_item = self.table.item(idx, 0)
            if t_item:
                t_item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
        self.is_updating_ui = False
        self.update_confirm_button()

    def update_confirm_button(self):
        active_count = sum(1 for p in self.polygon_items if p.is_active)
        grouped = any(object_type(p.cell_dict) == 'group' for p in self.polygon_items)
        self.btn_confirm.setText(f'Create New Analysis ({active_count})' if self.analysis_revision else
            f"Process Selected ({active_count})" if grouped else f"Process Selected Cells ({active_count} Selected)")
        self.btn_confirm.setEnabled(active_count > 0)

    def get_cells_data(self):
        """Formats confirmed cell polygons for the masking and kinematics engines."""
        selected_cells = []
        well_id = self.well_input.text().strip() or "Unknown"
        fov_id = self.fov_input.text().strip() or "FOV001"
        
        counter = 1
        for p in self.polygon_items:
            if not p.is_active: continue
            
            poly = p.cell_dict.get("polygon", [])
            if not poly: continue
            
            moments=cv2.moments(np.asarray(poly,dtype=np.float32))
            cx=moments['m10']/moments['m00'] if moments['m00'] else 0.
            cy=moments['m01']/moments['m00'] if moments['m00'] else 0.
            
            selected_cells.append({
                "id": p.cell_dict['id'],
                "object_type": object_type(p.cell_dict),
                "polygon": poly,
                "class_id": p.cell_dict.get("class_id", 0),
                "well_id": well_id,
                "fov_id": fov_id,
                "centroid_x": f"{cx:.1f}",
                "centroid_y": f"{cy:.1f}"
            })
            counter += 1
            
        return selected_cells

# --- 1.6 Micro-Manager Setup Wizard Handholding ---
# (ScannerWorker has been removed, as hardware setup is now fully self-contained)

class CameraIndexDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("DirectShow")
        self.setMinimumWidth(400)
        
        layout = QVBoxLayout(self)
        
        instructions = (
            "Choose a camera number and requested resolution. Use Autodetect to find available cameras. "
            "If the wrong camera opens, try another number."
        )
        lbl = QLabel(instructions)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        
        form = QFormLayout()
        
        idx_layout = QHBoxLayout()
        self.cam_index = QSpinBox()
        self.cam_index.setRange(0, 10)
        self.cam_index.setValue(0)
        
        self.btn_autodetect = QPushButton("Autodetect")
        set_button_color(self.btn_autodetect, '#F2994A')
        self.btn_autodetect.clicked.connect(self.autodetect_cameras)
        
        idx_layout.addWidget(self.cam_index)
        idx_layout.addWidget(self.btn_autodetect)
        
        self.res_combo = QComboBox()
        self.res_combo.addItems(["Default (Auto)", "1920x1080 (HD)", "3840x2160 (4K)", "4000x3000 (12MP)"])
        
        form.addRow("Camera number:", idx_layout)
        form.addRow("Requested resolution:", self.res_combo)
        layout.addLayout(form)
        
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)
        self.scan_error = ErrorDetails()
        layout.addWidget(self.scan_error)
        self._scan_busy = False
        
        self.btn_connect = QPushButton("Connect Camera")
        set_button_color(self.btn_connect, ACTION_GREEN)
        self.btn_connect.clicked.connect(self.accept)
        layout.addWidget(self.btn_connect)
        
    def autodetect_cameras(self):
        if self._scan_busy:
            return
        self._scan_busy = True
        self.scan_error.clear()
        self.btn_autodetect.setEnabled(False)
        self.btn_connect.setEnabled(False)
        self.cam_index.setEnabled(False)
        self.status_lbl.setText("Checking camera numbers 0–5…")
        api_preference = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY

        def scan():
            for i in range(6):
                cap = cv2.VideoCapture(i, api_preference)
                try:
                    if cap.isOpened():
                        return i
                finally:
                    cap.release()
            return -1

        self._scan_result = None
        self._scan_worker = TaskThread(scan, self)
        self._scan_worker.completed.connect(self.camera_scan_received)
        self._scan_worker.finished.connect(self.finish_camera_scan)
        self._scan_worker.start()

    def camera_scan_received(self, value, error):
        self._scan_result = (value, error)

    def finish_camera_scan(self):
        if not self._scan_worker.wait(0):
            QTimer.singleShot(20, self.finish_camera_scan)
            return
        self.on_camera_scan_finished(*self._scan_result)

    def reject(self):
        if self._scan_busy:
            self.status_lbl.setText('Waiting for camera detection to release its connections before closing.')
            return
        super().reject()

    def closeEvent(self, event):
        if self._scan_busy:
            self.status_lbl.setText('Waiting for camera detection to release its connections before closing.')
            event.ignore()
            return
        super().closeEvent(event)

    def on_camera_scan_finished(self, found_idx, error):
        self._scan_busy = False
        self.btn_autodetect.setEnabled(True)
        self.btn_connect.setEnabled(True)
        self.cam_index.setEnabled(True)
        if error is not None:
            self.status_lbl.clear()
            self.scan_error.set_error("Camera scan failed. Check the camera connection and retry Autodetect, or enter its camera number.", error)
        elif found_idx != -1:
            self.cam_index.setValue(found_idx)
            self.status_lbl.setText(f"Camera {found_idx} is available.")
        else:
            self.status_lbl.setText("No camera found at numbers 0–5. Check its connection and camera permissions, or enter another camera number.")

    def get_settings(self):
        return self.cam_index.value(), self.res_combo.currentText()


class HardwareWizardDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Camera Configuration")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)

        instructions = (
            "Choose the Micro-Manager adapter for your camera, then select its device model."
        )
        lbl = QLabel(instructions)
        lbl.setWordWrap(True)
        instructions_row = QHBoxLayout()
        instructions_row.addWidget(lbl,1)
        instructions_row.addWidget(InformationButton('Camera adapter names',
            'Adapter device names may differ from the model name printed on the camera.'),
            0,Qt.AlignmentFlag.AlignTop)
        layout.addLayout(instructions_row)

        form = QFormLayout()
        self.adapter_combo = QComboBox()
        self.adapter_combo.setToolTip("Select the software adapter (driver) corresponding to your camera brand.")

        self.device_combo = QComboBox()
        self.device_combo.setToolTip("Select the specific camera device component.")

        form.addRow("Device Adapter:", self.adapter_combo)
        form.addRow("Device Model:", self.device_combo)

        self.desc_lbl = QLabel("")
        self.desc_lbl.setWordWrap(True)
        form.addRow("", self.desc_lbl)

        layout.addLayout(form)

        self.btn_generate = QPushButton("Connect and Save Configuration")
        set_button_color(self.btn_generate, '#2D9CDB')
        self.btn_generate.clicked.connect(self.generate_config)
        self.btn_generate.setEnabled(False)
        layout.addWidget(self.btn_generate)

        self.status_lbl = QLabel("Fetching available adapters...")
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_lbl)
        self.hardware_error = ErrorDetails()
        layout.addWidget(self.hardware_error)
        self.btn_retry_adapters = QPushButton("Reload Adapters")
        self.btn_retry_adapters.clicked.connect(self.load_adapters)
        layout.addWidget(self.btn_retry_adapters)

        self.cfg_file = None

        self.adapter_combo.currentTextChanged.connect(self.on_adapter_changed)
        self.device_combo.currentIndexChanged.connect(self.on_device_changed)

        # Load adapters in a moment so UI can render
        QTimer.singleShot(100, self.load_adapters)

    def load_adapters(self):
        self.hardware_error.clear()
        self.adapter_combo.clear()
        self.btn_generate.setEnabled(False)
        if not HAS_MMCORE:
            self.status_lbl.setText("Micro-Manager is unavailable. Check its installation in Settings, then reload adapters.")
            return

        try:
            adapters = run_task(self, 'Loading camera adapters…',
                                lambda: sorted(list(get_configured_core().getDeviceAdapterNames())))
            self.adapter_combo.addItems(adapters)
            if not adapters:
                self.status_lbl.setText("No camera adapters found. Check the Micro-Manager adapter folder in Settings, then reload adapters.")
        except Exception as e:
            self.status_lbl.clear()
            self.hardware_error.set_error("Could not load camera adapters. Check the Micro-Manager installation and reload adapters.", e)

    def on_adapter_changed(self, adapter_name):
        self.hardware_error.clear()
        self.device_combo.clear()
        self.desc_lbl.setText("")
        if not adapter_name:
            self.btn_generate.setEnabled(False)
            return

        try:
            def find_cameras():
                pymmcore_plus = importlib.import_module("pymmcore_plus")
                DeviceType = getattr(pymmcore_plus, "DeviceType", None)
                if DeviceType is None:
                    raise RuntimeError("Unable to load DeviceType from pymmcore_plus")

                core = get_configured_core()

                all_devices = list(core.getAvailableDevices(adapter_name))

                try:
                    types = list(core.getAvailableDeviceTypes(adapter_name))
                except Exception:
                    types = [DeviceType.CameraDevice.value] * len(all_devices)

                try:
                    descs = list(core.getAvailableDeviceDescriptions(adapter_name))
                except Exception:
                    descs = [str(d) for d in all_devices]

                valid_cameras = []
                valid_descs = []
                for i in range(min(len(all_devices), len(types), len(descs))):
                    if types[i] == DeviceType.CameraDevice.value:
                        valid_cameras.append(str(all_devices[i]))
                        valid_descs.append(str(descs[i]))
                return valid_cameras, valid_descs

            valid_cameras, valid_descs = run_task(self, "Loading camera models…", find_cameras)

            if valid_cameras:
                for i in range(len(valid_cameras)):
                    self.device_combo.addItem(valid_cameras[i], userData=valid_descs[i])
                self.btn_generate.setEnabled(True)
                self.status_lbl.setText("Camera(s) found. Select to proceed.")
            else:
                self.btn_generate.setEnabled(False)
                self.status_lbl.setText("No cameras found for this adapter.")
        except Exception as e:
            self.status_lbl.clear()
            self.hardware_error.set_error(f"Could not load cameras for {adapter_name}. Choose another adapter or reload adapters to retry.", e)
            self.btn_generate.setEnabled(False)

    def on_device_changed(self, index):
        if index >= 0:
            desc = self.device_combo.itemData(index)
            if desc:
                self.desc_lbl.setTextFormat(Qt.TextFormat.PlainText)
                self.desc_lbl.setText(str(desc))
            else:
                self.desc_lbl.setText("")
        else:
            self.desc_lbl.setText("")

    def generate_config(self):
        adapter = self.adapter_combo.currentText()
        device = self.device_combo.currentText()

        if not adapter or not device:
            return

        self.btn_generate.setEnabled(False)
        self.hardware_error.clear()
        self.status_lbl.setText("Connecting camera…")

        acquired = {}
        try:
            def connect_camera():
                core = get_configured_core()
                acquired['core'] = core
                core.unloadAllDevices()
                core.loadDevice('Camera', adapter, device)
                core.initializeAllDevices()
                core.setCameraDevice('Camera')
                return core
            core = run_task(self, 'Connecting camera…', connect_camera)

            self.status_lbl.setText("Camera connected. Choose where to save the configuration.")

            save_path, _ = QFileDialog.getSaveFileName(self, "Save Configuration", "", "Config Files (*.cfg)")
            if save_path:
                run_task(self, 'Saving camera configuration…', lambda: core.saveSystemConfiguration(save_path))
                self.cfg_file = save_path
                self.accept()
            else:
                self.status_lbl.setText("Save cancelled.")
                run_task(self, 'Disconnecting camera…', core.unloadAllDevices)
                self.btn_generate.setEnabled(True)

        except Exception as e:
            self.status_lbl.clear()
            self.hardware_error.set_error("Could not connect and save the camera configuration. Check the camera connection and selected device, then retry.", e)
            self.btn_generate.setEnabled(True)
            try:
                if acquired:
                    run_task(self, 'Resetting camera connection…', acquired['core'].unloadAllDevices)
            except:
                pass

# --- 1.5 Custom Themed Popup ---
class CustomPopup(QDialog):
    """A custom styled dialog to replace the default OS message boxes."""
    def __init__(self, title, message, parent=None):
        super().__init__(parent)
        from tempo_ui import DIALOG_STYLE
        self.setWindowTitle('TEMPO · ' + title)
        self.setMinimumWidth(350)
        
        layout = QVBoxLayout(self)
        lbl = QLabel(message)
        lbl.setWordWrap(True)
        layout.addWidget(lbl)
        
        btn = QPushButton("OK")
        set_button_color(btn, '#2D9CDB')
        btn.clicked.connect(self.accept)
        layout.addWidget(btn)

# --- 2. The Worker Threads ---
class ProcessingWorker(QThread):
    log_signal = pyqtSignal(str)
    state_signal = pyqtSignal(str)
    result_signal = pyqtSignal(object)
    finished_signal = pyqtSignal()

    def __init__(self, task_type, filepath=None, cells_data=None, export_debug_avis=False, queue=None, retry_output=None, source_result=None):
        super().__init__()
        self.task_type, self.filepath, self.cells_data = task_type, filepath, cells_data
        self.export_debug_avis, self.queue, self.retry_output = export_debug_avis, queue, retry_output
        self.cancel_token = CancellationToken()
        self.kinetics_snapshot=copy.deepcopy(tempo_preferences.get_category('kinetics'))
        self.output_root = tempo_backend.PROCESSED_DIR
        self.source_result=copy.deepcopy(source_result)

    def cancel(self):
        self.cancel_token.cancel()
        self.state_signal.emit('Cancellation requested. Waiting for the current operation to settle…')

    def report(self, result):
        self.result_signal.emit(result)
        self.log_signal.emit(f'{os.path.basename(result.input_path)}: {result.outcome}' + (f' — {result.error}' if result.error else ''))

    def run(self):
        self.state_signal.emit('Processing…')
        try:
            if self.task_type == 'batch':
                result = tempo_backend.process_entire_queue(export_debug_avis=self.export_debug_avis,
                    cancel=self.cancel_token, queue=self.queue, on_result=self.report, output_root=self.output_root)
                if result.get('cancelled'):
                    status = 'Processing cancelled. Unfinished recordings remain queued.'
                elif result['failed']:
                    status = f"{result['completed']} recordings completed; {result['failed']} failed and remain queued."
                else:
                    status = f"Processing complete: {result['completed']} recordings resolved. Review individual outcomes below."
            elif self.task_type == 'geometry':
                import getpass
                from tempo_analysis_io import reanalyze_outlines
                result=reanalyze_outlines(self.source_result,self.cells_data,output_root=self.output_root,
                    export_videos=self.export_debug_avis,cancel=self.cancel_token,actor='local:'+getpass.getuser())
                self.report(result)
                status='Outline reanalysis complete.' if result.resolved else 'Outline reanalysis stopped. Previous results were preserved.'
            elif self.task_type == 'traces':
                from tempo_analysis_io import reanalyze_traces
                result=reanalyze_traces(self.filepath,self.kinetics_snapshot,self.cancel_token,output_root=self.output_root)
                self.report(result)
                status='Trace reanalysis complete.' if result.resolved else 'Trace reanalysis stopped. Previous results were preserved.'
            else:
                with tempo_backend.claim_queue_entries([self.filepath]):
                    result = tempo_backend.process_assay_result(self.filepath, cells_data=self.cells_data,
                        export_debug_avis=self.export_debug_avis, cancel=self.cancel_token, retry_output=self.retry_output, output_root=self.output_root)
                    if result.resolved and self.retry_output:
                        try:
                            tempo_backend.remove_from_queue([self.filepath], _settled=True)
                        except (OSError, ValueError) as exc:
                            result.outcome = 'failure'
                            result.error = f'Outputs retained, but the queue could not be updated: {exc}'
                self.report(result)
                status = {'failure': 'Processing failed. See Processing details before retrying.',
                          'cancelled': 'Processing cancelled. Partial outputs retained.',
                          'no_cells': 'Recording complete: no cells detected.',
                          'no_beats': 'Recording complete: no measurable beats.',
                          'success': 'Processing complete.'}[result.outcome]
            self.state_signal.emit(status)
        except Exception as exc:
            self.log_signal.emit(str(exc))
            self.state_signal.emit('Processing failed. See Processing details before retrying.')
        finally:
            self.finished_signal.emit()


class CameraAcquisitionWorker(QThread):
    """
    High-Speed Camera Acquisition Worker Thread (Milestone 2):
    Pops frames directly from Micro-Manager's circular buffer at native camera rate (100+ fps)
    directly to disk, completely decoupled from the 30Hz display preview timer.
    """
    progress_signal = pyqtSignal(int, float, float)  # frames_written, current_fps, buffer_free_pct
    finished_signal = pyqtSignal(str, int, float)     # output_path, total_frames, avg_fps
    error_signal = pyqtSignal(str)

    def __init__(self, core=None, cv_cap=None, output_path="", target_fps=100.0, max_frames=0,
                 native=False, recording_options=None,duration_s=None):
        super().__init__()
        self.core = core
        self.cv_cap = cv_cap
        self.output_path = output_path
        self.target_fps = float(target_fps) if target_fps > 0 else 100.0
        self.max_frames = int(max_frames)
        self.is_running = True
        self.stop_confirmed = True
        self.capture_hooks = None
        self.native = native
        self.recording_options = dict(recording_options or {})
        self.duration_s = None if duration_s is None else float(duration_s)

    def stop(self):
        self.is_running = False

    def run(self):
        writer = None
        quality_pool = quality_pending = None
        first_camera_time = first_receipt = None
        uses_core = bool(self.core and HAS_MMCORE)
        frames_written = 0
        started = last_frame = last_report = time.monotonic()
        report_frames = 0
        error = None
        try:
            if not uses_core and self.cv_cap is None:
                raise RuntimeError('No camera is connected.')
            if os.path.exists(self.output_path):
                raise FileExistsError('This recording already exists. Choose a new field or recording filename; the existing file was retained.')
            os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
            if uses_core:
                w, h = self.core.getImageWidth(), self.core.getImageHeight()
            else:
                w = int(self.cv_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(self.cv_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if w <= 0 or h <= 0:
                raise RuntimeError('The camera returned invalid image dimensions.')
            if self.native:
                import shutil
                from tempo_recording import NativeWriter,probe_storage
                if self.duration_s is not None and (not np.isfinite(self.duration_s) or self.duration_s<=0):
                    raise ValueError('Recording duration must be positive.')
                expected = self.max_frames or int(self.target_fps*(self.duration_s or 5))
                required = max(64*1024*1024,w*h*(2 if uses_core else 3)*max(10,expected))
                if shutil.disk_usage(os.path.dirname(self.output_path)).free < required:
                    raise OSError('Insufficient space for recoverable temporary footage. Choose another output location.')
                retention=dict(self.recording_options.get('retention',{}))
                if uses_core and hasattr(self.core,'getImageBitDepth'):
                    retention['sensor_bits']=int(self.core.getImageBitDepth())
                pixel_bytes=2 if retention.get('sensor_bits',8)>8 else 1
                throughput=probe_storage(os.path.dirname(self.output_path),w*h*(pixel_bytes if uses_core else 3)*self.target_fps)
                writer = NativeWriter(self.output_path,self.target_fps,**retention)
                writer.metadata.update(self.recording_options.get('metadata',{}))
                writer.metadata['requested_duration_s']=self.duration_s
                writer.metadata['duration_basis']='camera_elapsed_time_when_available_otherwise_host_receipt'
                writer.metadata['storage_write_check']=throughput
                from concurrent.futures import ThreadPoolExecutor
                quality_pool = ThreadPoolExecutor(max_workers=1,thread_name_prefix='capture-quality')
                writer.metadata['quality_sampling']='at_most_one_per_second'
            else:
                writer = cv2.VideoWriter(self.output_path, 0, self.target_fps, (w, h))
            if not self.native and not writer.isOpened():
                raise RuntimeError('Could not open the recording file. Check its location and available disk space.')
            if self.capture_hooks:
                self.capture_hooks[0](self.output_path)
            if uses_core and not self.core.isSequenceRunning():
                self.core.startContinuousSequenceAcquisition(0)
            while self.is_running and (not self.max_frames or frames_written < self.max_frames):
                camera_time = camera_id = None
                if uses_core:
                    if self.core.isBufferOverflowed():
                        raise RuntimeError('The camera buffer overflowed. Recording stopped; the partial file was retained.')
                    if not self.core.getRemainingImageCount():
                        if time.monotonic() - last_frame > 10:
                            raise RuntimeError('No camera frames received for 10 seconds. Recording stopped.')
                        time.sleep(0.001)
                        continue
                    if self.native and hasattr(self.core,'popNextTaggedImage'):
                        tagged = self.core.popNextTaggedImage()
                        frame = tagged.pix
                        camera_time = tagged.tags.get('ElapsedTime-ms')
                        camera_id = tagged.tags.get('FrameIndex')
                    else:
                        frame = self.core.popNextImage()
                    if frame.dtype == np.uint16 and not self.native:
                        frame = (frame >> 8).astype(np.uint8)
                    elif not self.native:
                        frame = frame.astype(np.uint8)
                    if frame.ndim == 2 and not self.native:
                        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                else:
                    ok, frame = self.cv_cap.read()
                    if not ok:
                        if not self.is_running:
                            break
                        raise RuntimeError('The camera stopped returning frames. The partial recording was retained.')
                if self.native:
                    if first_receipt is None:first_receipt=time.monotonic()
                    if camera_time is not None and first_camera_time is None:first_camera_time=float(camera_time)
                    writer.write(frame,camera_time_ms=camera_time,camera_frame_id=camera_id)
                    if quality_pending is not None and quality_pending.done():
                        try: writer.metadata.setdefault('quality_assessments',[]).append(quality_pending.result())
                        except Exception as exc: writer.metadata['quality_error']=str(exc)
                        quality_pending = None
                    if quality_pending is None and frames_written % max(1,int(self.target_fps)) == 0:
                        from tempo_quality import assess
                        signature=writer.metadata.get('quality_signature')
                        quality_pending=quality_pool.submit(assess,frame.copy(),signature,
                            self.recording_options.get('focus_profile'),frames_written,writer.metadata.get('sensor_bits'))
                else:
                    writer.write(frame)
                frames_written += 1
                report_frames += 1
                last_frame = time.monotonic()
                if self.native and self.duration_s is not None:
                    elapsed=((float(camera_time)-first_camera_time)/1000
                             if camera_time is not None and first_camera_time is not None else last_frame-first_receipt)
                    if elapsed>=self.duration_s:break
                    if last_frame-first_receipt>max(10.,self.duration_s*2):
                        raise RuntimeError('The camera timeline did not reach the requested duration; partial footage was retained.')
                if last_frame - last_report >= 0.5:
                    free = (100 * self.core.getBufferFreeCapacity() / max(1, self.core.getBufferTotalCapacity())) if uses_core else 100.0
                    self.progress_signal.emit(frames_written, report_frames / (last_frame - last_report), free)
                    last_report, report_frames = last_frame, 0
            if not frames_written:
                raise RuntimeError('Recording ended without saving any frames.')
        except Exception as exc:
            error = str(exc)
        finally:
            try:
                if uses_core and self.core.isSequenceRunning():
                    self.core.stopSequenceAcquisition()
            except Exception as exc:
                self.stop_confirmed = False
                error = error or f'Could not stop the camera: {exc}'
            try:
                if writer is not None:
                    if self.native:
                        if quality_pending is not None:
                            try: writer.metadata.setdefault('quality_assessments',[]).append(quality_pending.result(timeout=2))
                            except Exception as exc: writer.metadata['quality_error']=str(exc)
                        writer.close(error or '')
                    else:
                        writer.release()
            except Exception as exc:
                error = error or f'Could not finalize the recording: {exc}'
            if quality_pool is not None:
                quality_pool.shutdown(wait=False)
            if self.capture_hooks:
                try:
                    self.capture_hooks[1](frames_written, error or '')
                except Exception as exc:
                    error = error or f'Could not save capture metadata: {exc}'
        if error:
            self.error_signal.emit(error)
        else:
            self.finished_signal.emit(self.output_path, frames_written, frames_written / max(.001, time.monotonic() - started))

class CaptureTab(QWidget):
    cleanup_finished = pyqtSignal(bool)
    def __init__(self):
        super().__init__()
        self.core = None
        self.cv_cap = None
        self.camera_device = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self._preview_pending = False
        self._preview_task = None
        self._pending_camera_worker = None
        self._preview_wait_timer = QTimer(self)
        self._preview_wait_timer.setInterval(20)
        self._preview_wait_timer.timeout.connect(self.start_pending_camera_worker)
        self.is_recording = False
        self.video_writer = None
        self.acq_worker = None
        self.scout_worker = None
        self.setup_ui()

    def setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.capture_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.capture_splitter.setChildrenCollapsible(False)
        layout.addWidget(self.capture_splitter)

        # Each sidebar owns its controls; the center is exclusively imaging.
        left_panel = self.camera_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        # Hardware Controls (Micro-Manager)
        hw_group = QGroupBox("Camera")
        hw_layout = QFormLayout(hw_group)
        hw_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        self.cfg_path = QLineEdit()
        self.cfg_path.setPlaceholderText("Select .cfg file...")
        self.cfg_path.setReadOnly(True)
        self.btn_load_cfg = QPushButton("Browse")
        self.btn_load_cfg.clicked.connect(self.load_cfg)

        self.cfg_row = FilePickerRow(self.cfg_path,self.btn_load_cfg)

        self.btn_wizard = QPushButton("Camera Setup…")
        set_button_color(self.btn_wizard, '#F2994A')
        self.btn_wizard.clicked.connect(self.run_hardware_wizard)

        self.btn_cv_fallback = QPushButton("DirectShow")
        set_button_color(self.btn_cv_fallback, ACTION_GREEN)
        self.btn_cv_fallback.setToolTip('Connect a USB camera using DirectShow on Windows or the native camera backend on macOS.')
        self.btn_cv_fallback.clicked.connect(self.run_cv_fallback)

        self.spin_exposure = QSpinBox()
        self.spin_exposure.setRange(1, 5000)
        self.spin_exposure.setSuffix(" ms")
        self.spin_exposure.setEnabled(False)
        self.spin_exposure.valueChanged.connect(self.change_exposure)

        self.btn_cam = QPushButton("Start Hardware Feed")
        self.btn_cam.setEnabled(False)
        self.btn_cam.clicked.connect(self.toggle_camera)

        hw_layout.addRow(QLabel('Configuration file'))
        hw_layout.addRow(self.cfg_row)
        camera_setup_actions = FlowRow()
        camera_setup_actions.flow.addWidget(self.btn_wizard)
        camera_setup_actions.flow.addWidget(self.btn_cv_fallback)
        hw_layout.addRow(camera_setup_actions)
        hw_layout.addRow("Exposure:", self.spin_exposure)
        hw_layout.addRow(self.btn_cam)
        self.camera_settings_dialog = QDialog(self)
        self.camera_settings_dialog.setWindowTitle('TEMPO · Camera Connection and Properties')
        self.camera_settings_dialog.resize(520,400)
        settings_layout = QVBoxLayout(self.camera_settings_dialog)
        settings_layout.addWidget(hw_layout.itemAt(0).widget())
        settings_layout.addWidget(self.cfg_row)
        settings_layout.addWidget(camera_setup_actions)
        while hw_layout.count(): hw_layout.takeAt(0)
        self.btn_connection_settings = QPushButton('Camera Settings…')
        self.btn_connection_settings.clicked.connect(self.camera_settings_dialog.show)
        hw_layout.addRow(self.btn_connection_settings)
        hw_layout.addRow('Exposure:', self.spin_exposure)
        hw_layout.addRow(self.btn_cam)
        left_layout.addWidget(hw_group)        
        # Advanced Hardware Properties Form
        self.prop_group = QGroupBox("Advanced Camera Properties")
        self.prop_group.hide()
        self.prop_layout = QFormLayout(self.prop_group)
        
        self.prop_combo = QComboBox()
        self.prop_combo.setEnabled(False)
        self.prop_combo.currentTextChanged.connect(self.on_property_selected)
        self.prop_val_input = QLineEdit()
        self.prop_val_input.setEnabled(False)
        self.prop_val_input.returnPressed.connect(self.set_camera_property)
        self.btn_apply_prop = QPushButton("Apply")
        self.btn_apply_prop.setEnabled(False)
        self.btn_apply_prop.clicked.connect(self.set_camera_property)
        
        prop_val_layout = QHBoxLayout()
        prop_val_layout.addWidget(self.prop_val_input)
        prop_val_layout.addWidget(self.btn_apply_prop)
        
        self.prop_layout.addRow("Property:", self.prop_combo)
        self.prop_layout.addRow("Value:", prop_val_layout)
        settings_layout.addWidget(self.prop_group)
        
        # Metadata Filenaming Setup
        meta_group = QGroupBox("Well and Field")
        meta_layout = QFormLayout(meta_group)
        self.exp_id = QLineEdit("Assay001")
        self.well_id = QComboBox()
        for r in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']:
            for c in range(1, 13):
                self.well_id.addItem(f"{r}{c:02d}")
        self.fov_id = QSpinBox()
        self.fov_id.setRange(1, 999)
        self.z_index = QSpinBox()
        self.z_index.setRange(-999, 999)
        self.z_index.setValue(0)
        self.z_index.setEnabled(True)
        self.z_index.setToolTip('Manually recorded focus index; this is not a measured physical depth.')
        meta_layout.addRow("Experiment ID:", self.exp_id)
        meta_layout.addRow("Well ID:", self.well_id)
        meta_layout.addRow("FOV ID:", self.fov_id)
        meta_layout.addRow("Z-Index:", self.z_index)
        left_layout.addWidget(meta_group)
        
        # High-Speed Camera Acquisition (Milestone 2)
        cap_group = QGroupBox("Recording")
        cap_layout = QVBoxLayout(cap_group)
        self.chk_overlap = QCheckBox("Show Overlap Guide")
        self.chk_overlap.setChecked(bool(tempo_preferences.get("capture", "overlap_percentage", 25) > 0))
        cap_layout.addWidget(self.chk_overlap)
        
        self.btn_flir_preset = QPushButton("FLIR · 226 FPS")
        set_button_color(self.btn_flir_preset, '#1A5276')
        self.btn_flir_preset.setToolTip('Set a 226 FPS recording target and request 4 ms exposure when connected.')
        self.btn_flir_preset.clicked.connect(self.apply_flir_preset)
        cap_layout.addWidget(self.btn_flir_preset)

        self.btn_snap = QPushButton("Snapshot (.tif)")
        self.btn_snap.clicked.connect(self.capture_snapshot)
        self.btn_snap.setEnabled(False)
        cap_layout.addWidget(self.btn_snap)
        
        fps_layout = QFormLayout()
        self.spin_rec_fps = QSpinBox()
        self.spin_rec_fps.setRange(10, 226)
        init_fps = int(tempo_preferences.get("capture", "target_fps", 100))
        self.spin_rec_fps.setValue(min(226, max(10, init_fps)))
        self.spin_rec_fps.setSuffix(" fps")
        fps_layout.addRow('Target frame rate:', self.spin_rec_fps)
        cap_layout.addLayout(fps_layout)
        
        self.btn_record = QPushButton("Record Field")
        self.btn_record.clicked.connect(self.toggle_recording)
        self.btn_record.setEnabled(False)
        cap_layout.addWidget(self.btn_record)
        left_layout.addWidget(cap_group)
        self.retention_group = QGroupBox('Video retention')
        retention_layout = QVBoxLayout(self.retention_group)
        from tempo_ui import WrappingCheckBox
        self.save_original = WrappingCheckBox('Save Original Recordings')
        self.save_cell_videos = WrappingCheckBox('Save Intermediate Cell Videos')
        self.retained_format = QComboBox()
        self.retained_format.addItem('Native lossless','native')
        self.retained_format.addItem('8-bit compatibility','compatibility')
        self.retained_format.setEnabled(False)
        for check in (self.save_original,self.save_cell_videos):
            retention_layout.addWidget(check)
            check.toggled.connect(lambda _: self.retained_format.setEnabled(
                self.save_original.isChecked() or self.save_cell_videos.isChecked()))
        retention_layout.addWidget(self.retained_format)
        retention_note = QLabel('Results are kept. Unsaved footage is removed after successful processing.')
        retention_note.setWordWrap(True); retention_layout.addWidget(retention_note)
        left_layout.addWidget(self.retention_group)
        
        self.lbl_acq_status = QLabel("Acquisition Idle")
        self.lbl_acq_status.setVisible(False)
        self.lbl_acq_status.setWordWrap(True)
        # Recording actions remain beside the central image, independently of
        # setup-panel scrolling. Both side panels are user resizable.
        
        # Collection setup lives in the inspector; no uncalibrated plate scan.
        self.lbl_scout_status = QLabel()
        self.lbl_scout_status.setWordWrap(True)
        self.scout_progress_bar = QProgressBar()
        self.scout_progress_bar.hide()

        forms = (hw_layout, self.prop_layout, meta_layout, fps_layout)
        self._capture_forms = forms
        form_labels = [form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
                       for form in forms for row in range(form.rowCount())
                       if form.itemAt(row, QFormLayout.ItemRole.LabelRole) is not None]
        for form in forms:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setHorizontalSpacing(8)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        aligned_width = max(label.fontMetrics().horizontalAdvance(label.text()) for form in (hw_layout,meta_layout)
                            for row in range(form.rowCount())
                            if (item := form.itemAt(row,QFormLayout.ItemRole.LabelRole)) is not None
                            for label in (item.widget(),)) + 4
        for form in (hw_layout,meta_layout):
            for row in range(form.rowCount()):
                item = form.itemAt(row,QFormLayout.ItemRole.LabelRole)
                if item is not None: item.widget().setMinimumWidth(aligned_width)

        left_scroll = QScrollArea()
        left_panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        left_scroll.setMinimumWidth(300)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        left_scroll.setWidget(left_panel)
        self.acquisition_scroll = left_scroll
        self.cfg_path.setMinimumWidth(self.cfg_path.fontMetrics().horizontalAdvance(self.cfg_path.placeholderText()) + 12)
        self.camera_settings_dialog.resize(max(520,self.cfg_path.minimumWidth()+self.btn_load_cfg.sizeHint().width()+64),420)
        self._camera_preferred_minimum = max(300, aligned_width + self.spin_exposure.sizeHint().width() + 64)
        left_scroll.setMinimumWidth(self._camera_preferred_minimum)
        camera_sidebar = self.camera_panel = QWidget()
        sidebar_layout = QVBoxLayout(camera_sidebar)
        sidebar_layout.setContentsMargins(0,0,0,0)
        sidebar_layout.addWidget(left_scroll,1)
        capture_actions = FlowRow()
        cap_layout.removeWidget(self.btn_snap); cap_layout.removeWidget(self.btn_record)
        capture_actions.flow.addWidget(self.btn_snap); capture_actions.flow.addWidget(self.btn_record)
        sidebar_layout.addWidget(capture_actions)
        self.capture_splitter.addWidget(camera_sidebar)

        center = QWidget()
        feed_panel = QVBoxLayout(center)
        feed_panel.setContentsMargins(0, 0, 0, 0)
        self.feed_label = QLabel("No camera connected.\nLoad a hardware configuration or connect a USB camera.")
        self.feed_label.setWordWrap(True)
        self.feed_label.setMargin(8)
        self.feed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.feed_label.setMinimumSize(200, 180)
        self.feed_label.setStyleSheet('background-color: #1e1e1e; color: white; border: 1px solid #465467; font-size: 18px;')
        feed_panel.addWidget(self.feed_label, 1)
        left_layout.addWidget(self.lbl_acq_status)
        self.camera_error = ErrorDetails(); left_layout.addWidget(self.camera_error)
        left_layout.addWidget(self.lbl_scout_status)
        left_layout.addWidget(self.scout_progress_bar)
        self.capture_splitter.addWidget(center)

        from tempo_capture_ui import CaptureTools
        self.capture_controls = CaptureTools(self)
        for button in (self.btn_flir_preset,self.btn_wizard,self.btn_cv_fallback):
            button.setProperty('actionColor',None)
        self.btn_record.setProperty('primary',True)
        self.setStyleSheet('QGroupBox { border: none; border-top: 1px solid #3c4959; border-radius: 0; margin-top: 12px; padding-top: 6px; } QGroupBox::title { left: 0; padding: 0 5px 0 0; }')
        self.capture_controls.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        left_layout.addWidget(self.capture_controls.field_group)
        left_layout.addWidget(self.capture_controls.automatic_group)
        left_layout.addStretch()
        inspector_panel = self.stimulation_panel = QWidget()
        inspector_layout = QVBoxLayout(inspector_panel)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector = QScrollArea()
        inspector.setWidgetResizable(True)
        inspector.setMinimumWidth(270)
        inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector.setWidget(self.capture_controls)
        inspector_layout.addWidget(inspector, 1)
        # Stopping remains reachable even when a small window needs scrolling.
        self.capture_controls.layout().removeWidget(self.capture_controls.stop_button)
        inspector_layout.addWidget(self.capture_controls.stop_button)
        self.capture_inspector = inspector
        self.capture_splitter.addWidget(inspector_panel)
        self.capture_splitter.setStretchFactor(0, 0)
        self.capture_splitter.setStretchFactor(1, 1)
        self.capture_splitter.setStretchFactor(2, 0)
        saved_sizes = tempo_preferences.get('capture', 'panel_widths', [300, 650, 320])
        if not isinstance(saved_sizes, list) or len(saved_sizes) != 3 or not all(isinstance(n, int) and n > 0 for n in saved_sizes):
            saved_sizes = [300, 650, 320]
        self.capture_splitter.setSizes(saved_sizes)
        self.capture_splitter.splitterMoved.connect(self.capture_panels_resized)
        QTimer.singleShot(0, self.align_capture_forms)

    def capture_panels_resized(self, *args):
        self.align_capture_forms()
        tempo_preferences.set('capture', 'panel_widths', self.capture_splitter.sizes())
        if not hasattr(self, '_panel_save_timer'):
            self._panel_save_timer = QTimer(self)
            self._panel_save_timer.setSingleShot(True)
            self._panel_save_timer.timeout.connect(tempo_preferences.save_to_disk)
        self._panel_save_timer.start(400)

    def align_capture_forms(self):
        if not hasattr(self, '_capture_forms'):
            return
        if hasattr(self, 'capture_inspector'):
            available = self.width() - self.capture_inspector.minimumWidth() - 240
            self.acquisition_scroll.setMinimumWidth(min(self._camera_preferred_minimum,max(300,available)))
        for form in self._capture_forms:
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        forms = (self._capture_forms[0],self._capture_forms[2])
        required = max(item.widget().minimumWidth() for form in forms for row in range(form.rowCount())
                       if (item := form.itemAt(row,QFormLayout.ItemRole.LabelRole)) is not None)
        required += max(control.minimumSizeHint().width() for control in (self.spin_exposure,self.exp_id,self.well_id,self.fov_id)) + 8
        if self.acquisition_scroll.viewport().width() - 40 < required:
            for form in forms: form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        if hasattr(self, 'capture_inspector'):
            controls = self.capture_controls
            desired = controls.minimum_stimulation_width()
            available = self.width() - self.acquisition_scroll.minimumWidth() - 232
            self.capture_inspector.setMinimumWidth(min(max(270, desired), max(270, available)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.align_capture_forms()

    def apply_flir_preset(self):
        self.spin_rec_fps.setValue(226)
        if self.spin_exposure.isEnabled():
            self.spin_exposure.setValue(4)
        self.lbl_acq_status.setText('Recording target set to 226 fps.' + (' Exposure requested at 4 ms.' if self.spin_exposure.isEnabled() else ''))
        self.lbl_acq_status.setVisible(True)
        tempo_preferences.set("capture", "camera_profile", "FLIR_BFS_U3_16S2M_CS")
        tempo_preferences.set("capture", "target_fps", 226)
        if not tempo_preferences.save_to_disk():
            self.lbl_acq_status.setText(self.lbl_acq_status.text() + ' Settings could not be saved for the next session. Check profile folder permissions and apply the preset again.')

    def load_cfg(self):
        self.camera_error.clear()
        if not HAS_MMCORE:
            self.camera_error.set_error('Micro-Manager is unavailable. Open Guided Setup from Settings to check requirements, or use DirectShow.', RuntimeError('Micro-Manager Core could not be loaded.'))
            return

        file_path, _ = QFileDialog.getOpenFileName(self, "Select Micro-Manager Config", "", "Config Files (*.cfg)")
        if file_path:
            self.cfg_path.setText(file_path)
            self.load_cfg_from_path(file_path)

    def run_hardware_wizard(self):
        self.camera_error.clear()
        if not HAS_MMCORE:
            self.camera_error.set_error('Micro-Manager is unavailable. Open Guided Setup from Settings to check requirements, or use DirectShow.', RuntimeError('Micro-Manager Core could not be loaded.'))
            return
            
        self._camera_setup_busy = True
        try:
            # The dashboard must own this core even while the wizard/file chooser
            # is open, so application shutdown can release it in order.
            self.core = self.prepare_camera_setup(require_core=True)
            dialog = HardwareWizardDialog(self)
            if dialog.exec() == QDialog.DialogCode.Accepted and dialog.cfg_file:
                self.cfg_path.setText(dialog.cfg_file)
                self.load_cfg_from_path(dialog.cfg_file)
        except Exception as exc:
            self.camera_error.set_error('Camera setup could not finish. Check the connection and retry the configuration wizard.', exc)
        finally:
            self._camera_setup_busy = False

    def prepare_camera_setup(self, require_core=False):
        self.timer.stop()
        self.btn_snap.setEnabled(False)
        self.btn_record.setEnabled(False)
        core, cap = self.core, self.cv_cap
        released = []
        stop_state = {}
        def prepare():
            if core is not None:
                stop_state['unconfirmed'] = True
                if core.isSequenceRunning():
                    core.stopSequenceAcquisition()
                stop_state['unconfirmed'] = False
            if cap is not None:
                cap.release()
                released.append(True)
            return core or (get_configured_core() if require_core else None)
        try:
            return self.run_camera_task('Preparing camera setup', prepare)
        finally:
            if released:
                self.cv_cap = None
            if stop_state.get('unconfirmed'):
                self.mark_camera_stop_unconfirmed()
            else:
                CaptureTab.confirm_camera_stop(self)
                self.btn_cam.setText('Start Hardware Feed')
                self.feed_label.setText('Camera Feed Offline')
            
    def load_cfg_from_path(self, file_path):
        self.camera_error.clear()
        self.timer.stop()
        self.btn_snap.setEnabled(False)
        self.btn_record.setEnabled(False)
        try:
            old_core, old_cap = self.core, self.cv_cap
            acquired = {}
            def configure():
                if old_cap is not None:
                    old_cap.release()
                core = old_core or get_configured_core()
                acquired['core'] = core
                if core.isSequenceRunning():
                    core.stopSequenceAcquisition()
                core.loadSystemConfiguration(file_path)
                return core, core.getCameraDevice(), core.getExposure()
            try:
                self.core, self.camera_device, exp = self.run_camera_task('Loading camera configuration', configure)
            finally:
                if acquired:
                    self.core = acquired['core']
                    self.cv_cap = None
            # Fetch current exposure from camera firmware
            self.spin_exposure.blockSignals(True)
            self.spin_exposure.setValue(int(exp))
            self.spin_exposure.blockSignals(False)
            self.spin_exposure.setEnabled(True)
            
            # Dynamically fetch all exposed properties from the camera firmware
            try:
                core, device = self.core, self.camera_device
                props = self.run_camera_task('Reading camera properties', lambda: core.getDevicePropertyNames(device))
                                    
                self.prop_combo.blockSignals(True)
                self.prop_combo.clear()
                self.prop_combo.addItems(sorted(list(props)))
                self.prop_combo.blockSignals(False)
                self.prop_group.setVisible(self.prop_combo.count() > 0)
                self.prop_combo.setEnabled(True)
                self.on_property_selected(self.prop_combo.currentText())
            except Exception as e:
                self.prop_combo.clear()
                self.prop_group.hide()
                self.prop_combo.setEnabled(False)
                self.prop_val_input.clear()
                self.prop_val_input.setEnabled(False)
                self.btn_apply_prop.setEnabled(False)
                self.camera_error.set_error('Camera connected, but its advanced properties could not be loaded. Reload the camera configuration to retry.', e)
            
            self.btn_cam.setEnabled(True)
            self.btn_cam.setText('Start Hardware Feed')
            CaptureTab.confirm_camera_stop(self)
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText(f"Camera connected: {self.camera_device}")
            
        except Exception as e:
            self.camera_error.set_error('The camera configuration could not be loaded. Check the connected device and configuration file, then load it again.', e)

    def on_property_selected(self, prop_name):
        self.prop_val_input.clear()
        self.prop_val_input.setEnabled(False)
        self.btn_apply_prop.setEnabled(False)
        if not self.core or not prop_name: return
        try:
            core, device = self.core, self.camera_device
            val, allowed_list = self.run_camera_task('Reading camera property',
                lambda: (core.getProperty(device, prop_name), list(core.getAllowedPropertyValues(device, prop_name))))
            self.prop_val_input.setText(str(val))
            
            # Check if there are strict allowed values (for things like Binning: 1x1, 2x2)
            if allowed_list:
                # Display strict options so the user knows what they can type
                self.prop_val_input.setToolTip("Allowed Values:\n" + "\n".join(allowed_list))
                self.prop_val_input.setPlaceholderText("See Tooltip for Allowed Values")
            else:
                self.prop_val_input.setToolTip("")
                self.prop_val_input.setPlaceholderText("Enter New Value")
            self.prop_val_input.setEnabled(True)
            self.btn_apply_prop.setEnabled(True)
            if self.camera_error.summary.text() == 'Could not read the selected camera property. Select another property, or reselect this property to retry.':
                self.camera_error.clear()
        except Exception as e:
            self.prop_val_input.clear()
            self.camera_error.set_error('Could not read the selected camera property. Select another property, or reselect this property to retry.', e)

    def set_camera_property(self):
        prop_name = self.prop_combo.currentText()
        val = self.prop_val_input.text().strip()
        if not self.core or not prop_name or not val: return
        if getattr(self, '_feed_stop_pending', False): return
        self.camera_error.clear()
        try:
            core, device = self.core, self.camera_device
            def apply():
                core.setProperty(device, prop_name, val)
                return core.getProperty(device, prop_name)
            new_val = self.apply_camera_setting(apply)
            self.prop_val_input.setText(str(new_val))
            
            # If they changed Exposure manually via the advanced dropdown, sync the quick-spinbox
            if prop_name.lower() == "exposure":
                self.spin_exposure.blockSignals(True)
                self.spin_exposure.setValue(int(float(new_val)))
                self.spin_exposure.blockSignals(False)
                
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText(f"{prop_name}: {new_val}")
        except Exception as e:
            self.camera_error.set_error('The camera property could not be applied. Check its allowed values and retry.', e)

    def run_camera_task(self, title, action):
        """One camera owner at a time; all driver waits occur outside the GUI."""
        if getattr(self, '_camera_task_busy', False) or getattr(self, '_cleanup_pending', False):
            raise RuntimeError('The camera is still finishing another operation.')
        if any(worker is not None and not worker.wait(0) for worker in (self.acq_worker, self.scout_worker)):
            raise RuntimeError('Stop recording or plate scanning before changing the camera.')
        self._camera_task_busy = True
        was_preview = self.timer.isActive()
        self.timer.stop()
        preview_done = self._preview_task.done_event if self._preview_task is not None else None
        def perform():
            if preview_done is not None:
                preview_done.wait()
            return action()
        try:
            return run_task(self, title, perform)
        finally:
            self._camera_task_busy = False
            if was_preview and not getattr(self, '_feed_stop_pending', False) and not getattr(self, '_cleanup_pending', False):
                self.timer.start(30)

    def apply_camera_setting(self, action):
        core = self.core
        state = {'running': False, 'stopped': False}
        def apply():
            state['running'] = core.isSequenceRunning()
            try:
                if state['running']:
                    core.stopSequenceAcquisition()
                    state['stopped'] = True
                return action()
            finally:
                if state['stopped']:
                    try:
                        core.startContinuousSequenceAcquisition(0)
                    except Exception as exc:
                        state['restart_error'] = exc
        try:
            return self.run_camera_task('Applying camera setting', apply)
        finally:
            if state['running'] and not state['stopped']:
                self.mark_camera_stop_unconfirmed()
            if 'restart_error' in state:
                self.camera_restart_failed(state['restart_error'])

    def confirm_camera_stop(self):
        self._feed_stop_pending = False
        for name in ('acq_worker', 'scout_worker'):
            worker = getattr(self, name, None)
            if worker is not None:
                worker.stop_confirmed = True

    def mark_camera_stop_unconfirmed(self):
        self._feed_stop_pending = True
        self.timer.stop()
        self.btn_cam.setText('Retry Stop Feed')
        self.btn_snap.setEnabled(False); self.btn_record.setEnabled(False)
        self.feed_label.setText('Camera feed stop unconfirmed')

    def resume_camera_after_setting(self):
        try:
            core = self.core
            self.run_camera_task('Restarting camera preview', lambda: core.startContinuousSequenceAcquisition(0))
        except Exception as exc:
            self.camera_restart_failed(exc)

    def camera_restart_failed(self, exc):
        self.timer.stop()
        self.btn_cam.setText('Start Hardware Feed')
        self.btn_snap.setEnabled(False); self.btn_record.setEnabled(False)
        self.camera_error.set_error('The camera feed could not restart after changing a setting. Check the camera connection and choose Start Hardware Feed.', exc)

    apply_property_value = set_camera_property

    def change_exposure(self, value):
        if self.core:
            if getattr(self, '_feed_stop_pending', False): return
            self.camera_error.clear()
            try:
                core = self.core
                self.apply_camera_setting(lambda: core.setExposure(value))
            except Exception as e:
                self.camera_error.set_error('Exposure could not be applied. Check the camera limits and try another value.', e)

    def run_cv_fallback(self):
        self._camera_setup_busy = True
        try:
            self.prepare_camera_setup()
            dialog = CameraIndexDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            cam_idx, res_str = dialog.get_settings()
            self.camera_error.clear()
            try:
                self.timer.stop()
                self.prop_group.hide()
                self.btn_cam.setEnabled(False)
                self.feed_label.setText("Connecting...")
                old_core, old_cap = self.core, self.cv_cap
                acquired = {}
                api_preference = cv2.CAP_DSHOW if platform.system() == "Windows" else cv2.CAP_ANY
                def connect():
                    if old_core is not None:
                        if old_core.isSequenceRunning():
                            old_core.stopSequenceAcquisition()
                        old_core.unloadAllDevices()
                        acquired['core_released'] = True
                    if old_cap is not None:
                        old_cap.release()
                        acquired['cap_released'] = True
                    cap = cv2.VideoCapture(cam_idx, api_preference)
                    acquired['cap'] = cap
                    if not cap.isOpened():
                        raise RuntimeError(f'Camera index {cam_idx} did not open.')
                    for resolution, width, height in (('1920x1080', 1920, 1080), ('3840x2160', 3840, 2160), ('4000x3000', 4000, 3000)):
                        if resolution in res_str:
                            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                            break
                    return cap, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                try:
                    self.cv_cap, w, h = self.run_camera_task('Connecting camera', connect)
                finally:
                    if acquired.get('core_released'):
                        self.core = None
                    if acquired.get('cap_released'):
                        self.cv_cap = None
                    if 'cap' in acquired:
                        self.cv_cap = acquired['cap']

                # Disable exposure and advanced MM controls
                self.spin_exposure.setEnabled(False)
                self.prop_combo.clear()
                self.prop_combo.setEnabled(False)
                self.prop_val_input.setEnabled(False)
                self.btn_apply_prop.setEnabled(False)
                
                self.btn_cam.setEnabled(True)
                CaptureTab.confirm_camera_stop(self)
                self.lbl_acq_status.setText(f'USB camera connected · {w} × {h}. Exposure control is unavailable for this connection.')
                self.lbl_acq_status.show()
                
                # Start feed automatically
                if not self.timer.isActive():
                    self.toggle_camera()

            except Exception as e:
                self.camera_error.set_error('The USB camera connection did not finish. Review the details and retry DirectShow.', e)
        except Exception as exc:
            self.camera_error.set_error('Camera selection could not finish. Check the connection and retry DirectShow.', exc)
        finally:
            self._camera_setup_busy = False

    def toggle_camera(self):
        if self.core is None and self.cv_cap is None: return
        self.camera_error.clear()
        stopping = self.timer.isActive() or getattr(self, '_feed_stop_pending', False)
        if not stopping:
            try:
                if self.core:
                    core = self.core
                    self.run_camera_task('Starting camera preview', lambda: core.startContinuousSequenceAcquisition(0))
            except Exception as exc:
                self.btn_snap.setEnabled(False); self.btn_record.setEnabled(False)
                self.camera_error.set_error('The camera feed could not start. Check the camera connection and retry Start Hardware Feed.', exc)
                return
            self.btn_cam.setText("Stop Hardware Feed")
            self.btn_snap.setEnabled(True)
            self.btn_record.setEnabled(True)
            self.timer.start(30) # ~33 fps limit for GUI sync
        else:
            self.timer.stop()
            self.btn_snap.setEnabled(False)
            self.btn_record.setEnabled(False)
            try:
                core = self.core
                self.run_camera_task('Stopping camera preview', lambda: core.stopSequenceAcquisition() if core else None)
            except Exception as exc:
                self._feed_stop_pending = True
                self.btn_cam.setText('Retry Stop Feed')
                self.feed_label.setText('Camera feed stop unconfirmed')
                self.camera_error.set_error('The driver did not confirm that acquisition stopped. Choose Retry Stop Feed before starting another acquisition.', exc)
                return
            CaptureTab.confirm_camera_stop(self)
            self.btn_cam.setText("Start Hardware Feed")
            self.feed_label.setText("Camera Feed Offline")

    def generate_filename(self, ext):
        exp = self.exp_id.text().strip() or "Experiment"
        well = self.well_id.currentText()
        fov = self.fov_id.value()
        z = self.z_index.value()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{exp}_{well}_FOV{fov:03d}_Z{z}_{timestamp}.{ext}"

    def update_frame(self):
        if self.core is None and self.cv_cap is None: return
        if getattr(self, '_cleanup_pending', False) or self._preview_pending:
            return
        core, cap = self.core, self.cv_cap
        self._preview_pending = True
        def read():
            frame = None
            if time.monotonic()-getattr(self,'_last_quality_context_read',0)>.5:
                from tempo_capture_ui import optical_context
                try:self._preview_quality_context=optical_context(core,cap)
                except Exception:self._preview_quality_context={'quality_signature':None,'sensor_bits':None}
                self._last_quality_context_read=time.monotonic()
            if core:
                if core.getRemainingImageCount() > 0:
                    frame = core.getLastImage()
                    if frame is not None:
                        frame = frame.copy()  # Detach the camera buffer before handing it off.
            elif cap:
                ret, frame = cap.read()
                if not ret or frame is None:
                    raise RuntimeError('The USB camera stopped returning preview frames.')
            return frame
        self._preview_task = run_background(read, self.preview_received)

    def preview_received(self, frame, error):
        self._preview_pending = False
        self._preview_task = None
        if getattr(self, '_cleanup_pending', False) or getattr(self, '_cleanup_done', False):
            return
        if not self.timer.isActive():
            return  # A paused preview must not overwrite recording/scan feedback.
        try:
            if error:
                raise error
            if frame is None: return
            if hasattr(self, 'capture_controls'):
                context=getattr(self,'_preview_quality_context',None)
                if context is not None:self.capture_controls.preview_context(context)
                self.capture_controls.preview_for_counting(frame)
            
            if len(frame.shape) == 2:
                h, w = frame.shape
            else:
                h, w, _ = frame.shape
            
            if frame.dtype == np.uint16:
                # Stable fixed bit-shift to prevent per-frame baseline intensity flicker
                if not hasattr(self, '_sensor_shift'):
                    f_max = frame.max()
                    if f_max > 4095:
                        self._sensor_shift = 8
                    elif f_max > 1023:
                        self._sensor_shift = 4
                    elif f_max > 255:
                        self._sensor_shift = 2
                    else:
                        self._sensor_shift = 0
                frame_8bit = (frame >> self._sensor_shift).clip(0, 255).astype(np.uint8)
            else:
                frame_8bit = frame.astype(np.uint8)

            if len(frame_8bit.shape) == 2:
                display_frame = cv2.cvtColor(frame_8bit, cv2.COLOR_GRAY2BGR)
            else:
                display_frame = frame_8bit.copy()

            if self.chk_overlap.isChecked():
                overlay = display_frame.copy()
                color = (0, 255, 0)
                thick = 2
                overlap = min(40, max(0, tempo_preferences.get('capture', 'overlap_percentage', 25))) / 100
                mx, my = int(w * overlap), int(h * overlap)
                
                cv2.line(overlay, (mx, 0), (mx, h), color, thick)
                cv2.line(overlay, (w - mx, 0), (w - mx, h), color, thick)
                cv2.line(overlay, (0, my), (w, my), color, thick)
                cv2.line(overlay, (0, h - my), (w, h - my), color, thick)
                
                cx, cy = w // 2, h // 2
                cv2.line(overlay, (cx - 20, cy), (cx + 20, cy), color, thick)
                cv2.line(overlay, (cx, cy - 20), (cx, cy + 20), color, thick)
                
                cv2.addWeighted(overlay, 0.5, display_frame, 0.5, 0, display_frame)

            frame_rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
            bytes_per_line = 3 * w
            qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(qimg)
            self.feed_label.setPixmap(pixmap.scaled(self.feed_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

        except Exception as e:
            self.timer.stop()
            self.btn_snap.setEnabled(False)
            self.btn_record.setEnabled(False)
            if self.core:
                self.mark_camera_stop_unconfirmed()
                recovery = 'Choose Retry Stop Feed before restarting the preview.'
            else:
                self.btn_cam.setText('Start Hardware Feed')
                self.feed_label.setText('Camera preview unavailable')
                recovery = 'Check the camera connection and choose Start Hardware Feed to retry.'
            self.camera_error.set_error('The camera preview stopped. ' + recovery, e)
            if hasattr(self, 'capture_controls'):
                self.capture_controls.capture_failure('Camera preview failed: ' + str(e))

    def start_pending_camera_worker(self):
        if getattr(self, '_cleanup_pending', False) or getattr(self, '_cleanup_done', False):
            self._preview_wait_timer.stop()
            return
        if getattr(self, '_preview_pending', False):
            self._preview_wait_timer.start()
            return
        self._preview_wait_timer.stop()
        worker, self._pending_camera_worker = self._pending_camera_worker, None
        if worker is not None and worker.is_running:
            worker.start()

    def capture_snapshot(self):
        self.camera_error.clear()
        if self.core is None and self.cv_cap is None:
            self.lbl_acq_status.setText('Connect a camera before taking a snapshot.')
            self.lbl_acq_status.show()
            return
        try:
            core, cap = self.core, self.cv_cap
            def read():
                if core:
                    if core.isSequenceRunning():
                        frame = core.getLastImage()
                    else:
                        core.snapImage()
                        frame = core.getImage()
                else:
                    ret, frame = cap.read()
                    if not ret:
                        frame = None
                if frame is None:
                    raise RuntimeError('The camera did not return a frame.')
                return frame.copy()
            frame = self.run_camera_task('Taking snapshot', read)
            # Ensure memory contiguity before handing to OpenCV's C++ writer
            frame = np.ascontiguousarray(frame)
            
            exp_folder = self.exp_id.text().strip() or "Experiment"
            filename = self.generate_filename("tif")
            session_dir = os.path.join(tempo_backend.RAW_DIR, exp_folder)
            os.makedirs(session_dir, exist_ok=True)
            path = os.path.join(session_dir, filename)
            
            if not cv2.imwrite(path, frame):
                raise RuntimeError('The snapshot could not be written. Check the destination and available disk space.')
            bit_depth = "16-bit" if frame.dtype == np.uint16 else "8-bit"
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText(f"Snapshot saved ({bit_depth}): {filename}")
            self.fov_id.setValue(self.fov_id.value() + 1)
        except Exception as e:
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText('Snapshot was not saved.')
            self.camera_error.set_error('Could not save a snapshot. Check the camera connection and output-folder access, then retry.', e)

    def toggle_recording(self):
        controls = getattr(self, 'capture_controls', None)
        worker = getattr(self, 'acq_worker', None)
        if controls is not None and not (worker and (worker.isRunning() or self._pending_camera_worker is worker)):
            controls.request_record()
        else:
            CaptureTab._toggle_recording(self)

    def _toggle_recording(self):
        """Toggles high-speed camera acquisition on a dedicated background worker thread."""
        if self.acq_worker and (self.acq_worker.isRunning() or self._pending_camera_worker is self.acq_worker):
            self.acq_worker.stop()
            if self.cancel_pending_camera_worker(self.acq_worker):
                self.is_recording = False
                self.btn_record.setText('Record Field')
                set_button_color(self.btn_record, '')
                self.lbl_acq_status.setText('Recording cancelled before capture started.')
                return
            self.btn_record.setText("Stopping Acquisition...")
            self.btn_record.setEnabled(False)
            return

        if self.scout_worker and (self.scout_worker.isRunning() or self._pending_camera_worker is self.scout_worker):
            self.lbl_acq_status.setText("Stop the plate scan before recording manually.")
            return
        if any(worker is not None and not worker.wait(0) for worker in (self.acq_worker, self.scout_worker)):
            self.lbl_acq_status.setText('Waiting for the previous acquisition to finish releasing its resources.')
            return
        if self.core is None and self.cv_cap is None:
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText('Connect a camera before recording.')
            return

        self.camera_error.clear()
        try:
            exp_folder = self.exp_id.text().strip() or "Experiment"
            filename = self.generate_filename("tif")
            session_dir = os.path.join(tempo_backend.RAW_DIR, '.capture-cache',exp_folder)
            os.makedirs(session_dir, exist_ok=True)
            path = os.path.join(session_dir, filename)
        except OSError as exc:
            self.lbl_acq_status.show()
            self.lbl_acq_status.setText('Recording was not started.')
            self.camera_error.set_error('Could not create the recording folder. Check output-folder permissions and available disk space, then retry.', exc)
            return

        target_fps = float(self.spin_rec_fps.value())
        self.acq_worker = CameraAcquisitionWorker(
            core=self.core,
            cv_cap=self.cv_cap,
            output_path=path,
            target_fps=target_fps,
            max_frames=0 # Continuous recording until user clicks stop
            ,native=True,recording_options=self.recording_options()
        )
        controls = getattr(self, 'capture_controls', None)
        if controls is not None:
            self.acq_worker.capture_hooks = controls.recording_hooks()
        self.acq_worker.progress_signal.connect(self.on_acq_progress)
        self.acq_worker.finished_signal.connect(self.on_acq_finished)
        self.acq_worker.error_signal.connect(self.on_acq_error)

        self.is_recording = True
        self.btn_record.setText("Stop Recording")
        self.btn_record.setEnabled(True)
        set_button_color(self.btn_record, '#E74C3C')
        self.lbl_acq_status.setText("Recording…")
        self.lbl_acq_status.setVisible(True)
        self.acq_worker.finished.connect(self.resume_camera_controls)
        self.pause_camera_controls()
        self._pending_camera_worker = self.acq_worker
        self.start_pending_camera_worker()

    def pause_camera_controls(self):
        self._preview_was_active = self.timer.isActive()
        self.timer.stop()
        names = ('btn_cam', 'btn_snap', 'btn_load_cfg', 'btn_wizard', 'btn_cv_fallback',
                 'btn_flir_preset', 'spin_exposure', 'prop_combo', 'prop_val_input', 'btn_apply_prop',
                 'save_original','save_cell_videos','retained_format','z_index')
        self._camera_control_states = [(getattr(self, name), getattr(self, name).isEnabled()) for name in names]
        for control, _ in self._camera_control_states:
            control.setEnabled(False)

    def cancel_pending_camera_worker(self, worker):
        if self._pending_camera_worker is not worker:
            return False
        self._pending_camera_worker = None
        self._preview_wait_timer.stop()
        self.resume_camera_controls()
        return True

    def resume_camera_controls(self):
        if getattr(self, '_cleanup_pending', False):
            return
        if getattr(self, '_preview_pending', False):
            QTimer.singleShot(50, self.resume_camera_controls)
            return
        if any(isinstance(worker, QThread) and not worker.wait(0) for worker in
               (getattr(self, 'acq_worker', None), getattr(self, 'scout_worker', None))):
            QTimer.singleShot(50, self.resume_camera_controls)
            return
        for control, enabled in getattr(self, '_camera_control_states', []):
            control.setEnabled(enabled)
        self._camera_control_states = []
        if (getattr(self, '_feed_stop_pending', False) or any(
                not getattr(getattr(self, name, None), 'stop_confirmed', True)
                for name in ('acq_worker', 'scout_worker'))):
            self.mark_camera_stop_unconfirmed()
            self.camera_error.set_error('Acquisition ended, but the driver did not confirm that the camera stopped. Choose Retry Stop Feed before another acquisition.',
                                        RuntimeError('Camera stop was not confirmed.'))
            self._preview_was_active = False
            return
        self.btn_record.setEnabled(bool(getattr(self, 'core', None) or getattr(self, 'cv_cap', None)))
        if getattr(self, '_preview_was_active', False):
            try:
                core = self.core
                def resume():
                    if core and not core.isSequenceRunning():
                        core.startContinuousSequenceAcquisition(0)
                self.run_camera_task('Resuming camera preview', resume)
                self.timer.start(33)
            except Exception as exc:
                self.timer.stop()
                self.btn_snap.setEnabled(False)
                self.btn_record.setEnabled(False)
                self.btn_cam.setText('Start Hardware Feed')
                self.camera_error.set_error('The camera preview could not restart after recording. Check the camera connection and choose Start Hardware Feed.', exc)
        self._preview_was_active = False

    def on_acq_progress(self, frames, current_fps, buffer_free_pct):
        self.lbl_acq_status.setText(f"Recorded: {frames} frames ({current_fps:.1f} fps) | Buffer: {buffer_free_pct:.0f}% free")

    def on_acq_finished(self, out_path, total_frames, avg_fps):
        self.is_recording = False
        self.btn_record.setText("Record Field")
        set_button_color(self.btn_record, '')
        self.btn_record.setEnabled(True)
        self.lbl_acq_status.setText(f"Acquisition Complete: {total_frames} frames ({avg_fps:.1f} avg fps)")
        self.fov_id.setValue(self.fov_id.value() + 1)
        controls = getattr(self, 'capture_controls', None)
        if controls is not None:
            controls.recording_finished()
        tempo_backend.add_to_queue(out_path)
        window = self.window()
        if hasattr(window,'start_captured_processing'):
            window.start_captured_processing([out_path])
        # Retain the worker until its QThread has finished.

    def recording_options(self):
        controls = getattr(self,'capture_controls',None)
        context = getattr(controls,'_record_context',None) or {}
        return dict(retention=dict(retain_original=self.save_original.isChecked(),
            retain_cells=self.save_cell_videos.isChecked(),export_format=self.retained_format.currentData(),
            owned=True,z_index=self.z_index.value()),
            metadata=dict(optical_signature=context.get('signature'),quality_signature=context.get('quality_signature')),
            focus_profile=getattr(controls,'focus_profile',None))

    def on_acq_error(self, err_msg):
        self.is_recording = False
        self.btn_record.setText("Record Field")
        set_button_color(self.btn_record, '')
        self.btn_record.setEnabled(True)
        self.lbl_acq_status.setText(f"Acquisition Error: {err_msg}")
        controls = getattr(self, 'capture_controls', None)
        if controls is not None:
            controls.recording_finished(err_msg)
        # Retain the worker until its QThread has finished.

    def toggle_plate_scout(self):
        self.capture_controls.start_automatic()

    def start_plate_scout(self):
        self.capture_controls.start_automatic()

    def stop_plate_scout(self):
        if self.scout_worker is not None:
            self.scout_worker.stop()
            self.cancel_pending_camera_worker(self.scout_worker)

    def cleanup(self):
        """Start ordered shutdown without blocking the Qt event loop."""
        if getattr(self, '_cleanup_done', False):
            return True
        if getattr(self, '_cleanup_pending', False):
            return False
        controls = getattr(self, 'capture_controls', None)
        if controls is not None and not controls.shutdown():
            QTimer.singleShot(50, self.cleanup)
            return False
        self._cleanup_pending = True
        self.timer.stop()
        self._preview_wait_timer.stop()
        self.setEnabled(False)
        self.lbl_acq_status.setText('Stopping acquisition and releasing the camera…')
        workers = [w for w in (self.acq_worker, self.scout_worker) if w is not None]
        for worker in workers:
            worker.stop()
        self._pending_camera_worker = None
        preview_done = self._preview_task.done_event if self._preview_task is not None else None
        core, cap = self.core, self.cv_cap
        if not any(w.isRunning() for w in workers) and core is None and cap is None:
            self._cleanup_done = True
            self._cleanup_pending = False
            return True
        def release():
            if preview_done is not None:
                preview_done.wait()
            for worker in workers:
                worker.wait()
            if core is not None:
                if core.isSequenceRunning():
                    core.stopSequenceAcquisition()
                core.unloadAllDevices()
            if cap is not None:
                cap.release()
        self._cleanup_worker = TaskThread(release, self)
        self._cleanup_error = None
        def completed(value, error):
            self._cleanup_error = error
        self._cleanup_worker.completed.connect(completed)
        self._cleanup_worker.finished.connect(self.finish_cleanup)
        self._cleanup_worker.start()
        return False

    def finish_cleanup(self):
        self._cleanup_pending = False
        if self._cleanup_error:
            self.setEnabled(True)
            self.camera_error.set_error('Camera shutdown could not finish. Retry Quit after resolving the connection.', self._cleanup_error)
            self.cleanup_finished.emit(False)
            return
        self.core = self.cv_cap = None
        self._cleanup_done = True
        self.cleanup_finished.emit(True)

from PyQt6.QtGui import QPolygonF
from tempo_nuclei import is_nucleus, migrate_record, supplement
from tempo_preprocessing import preview_image


class AnnotationView(QGraphicsView):
    polygon_drawn = pyqtSignal(list)
    drawing_status = pyqtSignal(str)
    mask_selected = pyqtSignal(int, bool)
    
    def __init__(self):
        super().__init__()
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.image_item = None
        self._image_path = None
        self._is_panning = False
        self._fit_on_resize = True
        self._pan_start_pos = None
        self.draw_pen = QPen(QColor(0, 255, 0), 2)
        
        self.draw_mode = "polygon" # "polygon" or "box"
        self.current_polygon_pts = []
        self.temp_polygon_item = None
        
        self.box_start_pos = None
        self.temp_box_item = None
        self.mask_items = []
        self._mask_pick_cycle = None
        self._mask_selection_press = False
        
        self.setMouseTracking(True)
        
    def set_draw_mode(self, mode):
        if self.draw_mode == mode:
            return
        self.draw_mode = mode
        self._cleanup_drawing()
        
    def set_image(self, filepath):
        if self._image_path == filepath and self.image_item is not None:
            return
        self._image_path = filepath
        self._fit_on_resize = True
        self._cleanup_drawing()
        self.set_mask_items([])
        self.scene.clear()
        pixmap = QPixmap.fromImage(preview_image(filepath, getattr(self, '_enhanced', False)))
        self.image_item = self.scene.addPixmap(pixmap)
        self.setSceneRect(0, 0, pixmap.width(), pixmap.height())
        QTimer.singleShot(0, self.fit_image)

    def set_contrast(self, enhanced):
        self._enhanced = enhanced
        if self.image_item is not None and self._image_path:
            self.image_item.setPixmap(QPixmap.fromImage(preview_image(self._image_path,enhanced)))

    def fit_image(self):
        if self.image_item is None:
            return
        self._mask_pick_cycle = None
        self._fit_on_resize = True
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, '_fit_on_resize', False):
            QTimer.singleShot(0, self.fit_image)

    def zoom_image(self, factor):
        self._mask_pick_cycle = None
        self._fit_on_resize = False
        self.scale(factor, factor)
        
    def set_draw_pen(self, pen):
        self.draw_pen = pen
        
    def _cleanup_drawing(self):
        self.current_polygon_pts = []
        self.drawing_status.emit('')
        if self.temp_polygon_item:
            if self.temp_polygon_item.scene() == self.scene:
                self.scene.removeItem(self.temp_polygon_item)
            self.temp_polygon_item = None
        self.box_start_pos = None
        if self.temp_box_item:
            if self.temp_box_item.scene() == self.scene:
                self.scene.removeItem(self.temp_box_item)
            self.temp_box_item = None
            
    def _update_temp_drawing(self, cursor_pos=None):
        if self.temp_polygon_item:
            if self.temp_polygon_item.scene() == self.scene:
                self.scene.removeItem(self.temp_polygon_item)
            
        pts = self.current_polygon_pts.copy()
        if cursor_pos and pts:
            pts.append(cursor_pos)
            
        if pts:
            qpoly = QPolygonF([QPointF(p[0], p[1]) for p in pts])
            import PyQt6.QtWidgets as QtWidgets
            self.temp_polygon_item = QtWidgets.QGraphicsPolygonItem(qpoly)
            self.temp_polygon_item.setPen(self.draw_pen)
            brush_color = self.draw_pen.color()
            brush_color.setAlpha(80)
            self.temp_polygon_item.setBrush(brush_color)
            self.scene.addItem(self.temp_polygon_item)
            
    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.zoom_image(1.15)
        else:
            self.zoom_image(1 / 1.15)
            
    def set_mask_items(self, items):
        self.mask_items = list(items)
        self._mask_pick_cycle = None

    def _select_mask_at(self, position, additive):
        # Test in viewport coordinates so edge tolerance stays three pixels at any zoom.
        point = QPointF(position)
        candidates = []
        stroker = QPainterPathStroker()
        stroker.setWidth(6)
        for index, item in enumerate(self.mask_items):
            if not item.isVisible():
                continue
            polygon = self.viewportTransform().map(item.mapToScene(item.polygon()))
            path = QPainterPath()
            path.addPolygon(polygon)
            path.closeSubpath()
            inside = path.contains(point)
            if not inside and not stroker.createStroke(path).contains(point):
                continue
            points = list(polygon)
            area = abs(sum(p.x()*q.y() - q.x()*p.y()
                           for p, q in zip(points, points[1:] + points[:1]))) / 2
            candidates.append((not inside, area, index))
        indices = tuple(candidate[2] for candidate in sorted(candidates))
        if not indices:
            self._mask_pick_cycle = None
            return
        cycle = self._mask_pick_cycle
        offset = 0
        if cycle and cycle[1] == indices and (position - cycle[0]).manhattanLength() <= 4:
            offset = (cycle[2] + 1) % len(indices)
        self._mask_pick_cycle = (QPoint(position), indices, offset)
        self.mask_selected.emit(indices[offset], additive)

    def mousePressEvent(self, event):
        # Qt maps ControlModifier to Command on macOS.
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._mask_selection_press = True
            self._select_mask_at(event.pos(), bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            event.accept()
            return
        self._mask_pick_cycle = None
        if event.button() == Qt.MouseButton.MiddleButton:
            self._fit_on_resize = False
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
            
        if self.draw_mode == "none":
            super().mousePressEvent(event)
            return

        if self.draw_mode == "polygon":
            if event.button() == Qt.MouseButton.RightButton:
                pts = self.current_polygon_pts
                area = abs(sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(pts, pts[1:]+pts[:1]))) / 2 if pts else 0
                if len({tuple(p) for p in pts}) < 3 or area <= 1e-12:
                    self.drawing_status.emit('Add at least three corners enclosing an area, then right-click to finish.')
                else:
                    self.polygon_drawn.emit([p[:] for p in pts])
                    self._cleanup_drawing()
                event.accept()
                return
            elif event.button() == Qt.MouseButton.LeftButton and self.image_item:
                pos = self.mapToScene(event.pos())
                point = [pos.x(), pos.y()]
                if self.image_item.boundingRect().contains(pos) and (not self.current_polygon_pts or point != self.current_polygon_pts[-1]):
                    self.current_polygon_pts.append(point)
                    self.drawing_status.emit('')
                self._update_temp_drawing([pos.x(), pos.y()])
                event.accept()
                return
        else:
            # Box mode
            if event.button() == Qt.MouseButton.LeftButton and self.image_item:
                self.box_start_pos = self.mapToScene(event.pos())
                import PyQt6.QtWidgets as QtWidgets
                self.temp_box_item = QtWidgets.QGraphicsRectItem(QRectF(self.box_start_pos, self.box_start_pos))
                self.temp_box_item.setPen(self.draw_pen)
                brush_color = self.draw_pen.color()
                brush_color.setAlpha(80)
                self.temp_box_item.setBrush(brush_color)
                self.scene.addItem(self.temp_box_item)
                event.accept()
                return
                
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and (self.draw_mode == 'polygon'
                     or event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            self.mousePressEvent(event)
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self.current_polygon_pts or self.temp_box_item):
            self._cleanup_drawing()
            event.accept()
            return
        super().keyPressEvent(event)
        
    def mouseMoveEvent(self, event):
        if self._mask_selection_press:
            event.accept()
            return
        if self._is_panning:
            delta = event.pos() - self._pan_start_pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan_start_pos = event.pos()
            event.accept()
            return
            
        if self.draw_mode == "none":
            super().mouseMoveEvent(event)
            return

        if self.draw_mode == "polygon":
            if self.current_polygon_pts:
                pos = self.mapToScene(event.pos())
                self._update_temp_drawing([pos.x(), pos.y()])
        else:
            if self.temp_box_item and self.box_start_pos:
                curr_pos = self.mapToScene(event.pos())
                r = QRectF(self.box_start_pos, curr_pos).normalized()
                self.temp_box_item.setRect(r)
                
        super().mouseMoveEvent(event)
        
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._mask_selection_press:
            self._mask_selection_press = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            self._is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        if self.draw_mode == "none":
            super().mouseReleaseEvent(event)
            return

        if self.draw_mode == 'polygon' and event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            event.accept()
            return
            
        if self.draw_mode == "box":
            if event.button() == Qt.MouseButton.LeftButton and self.temp_box_item:
                r = self.temp_box_item.rect()
                if r.width() > 5 and r.height() > 5:
                    pts = [
                        [r.x(), r.y()],
                        [r.x() + r.width(), r.y()],
                        [r.x() + r.width(), r.y() + r.height()],
                        [r.x(), r.y() + r.height()]
                    ]
                    self.polygon_drawn.emit(pts)
                if self.temp_box_item.scene() == self.scene:
                    self.scene.removeItem(self.temp_box_item)
                self.temp_box_item = None
                self.box_start_pos = None
                
        super().mouseReleaseEvent(event)

class UniversalImportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image Details")
        self.layout = QVBoxLayout(self)
        
        self.layout.addWidget(QLabel("Magnification (optional):"))
        self.mag_input = QLineEdit()
        self.layout.addWidget(self.mag_input)
        
        self.layout.addWidget(QLabel("Well Number (optional):"))
        self.well_input = QLineEdit()
        self.layout.addWidget(self.well_input)
        
        self.btn_ok = QPushButton("Import")
        self.btn_ok.clicked.connect(self.accept)
        self.layout.addWidget(self.btn_ok)
        
    def get_data(self):
        return self.mag_input.text().strip(), self.well_input.text().strip()


class AutoAnnotateWorker(QThread):
    status_signal = pyqtSignal(str)
    stage_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(dict)
    error_signal = pyqtSignal(str)

    def __init__(self, current_file, model_type="cellpose", parent=None):
        super().__init__(parent)
        self.current_file = current_file
        self.model_type = model_type

    def run(self):
        try:
            self.status_signal.emit('Loading model…')
            conf_val = float(tempo_preferences.get("annotator", "confidence_threshold", 0.15))
            iou_val = float(tempo_preferences.get("annotator", "nms_iou_threshold", 0.40))
            
            from tempo_assistance_runtime import run_assistance
            json_response = run_assistance(self.current_file, self.model_type, conf_val, iou_val,
                                           progress=self.status_signal.emit, stage=self.stage_signal.emit)
                    
            if not json_response:
                raise RuntimeError('The model worker did not return a result.')
                
            if not json_response.get("success"):
                raise Exception(json_response.get("error", "Unknown error"))
                
            self.finished_signal.emit(json_response)
        except Exception as e:
            self.error_signal.emit(str(e))


class ExportAndTrainWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, is_universal_mode, universal_annotations, annotations, class_config, base_dir, parent=None):
        super().__init__(parent)
        self.is_universal_mode = is_universal_mode
        self.universal_annotations = universal_annotations
        self.annotations = annotations
        self.class_config = class_config
        self.base_dir = base_dir

    def run(self):
        try:
            from tempo_training import build_dataset, select_approved_annotations
            self.progress_signal.emit(5, 'Preparing isolated training, validation and holdout splits…')
            if self.is_universal_mode:
                if not tempo_hub.is_maintainer():
                    raise PermissionError('Only a maintainer can train the approved master dataset.')
                revision = tempo_hub._head()
                records = tempo_hub._read('universal_annotations.json', revision)
                records = select_approved_annotations(records)
                for name in records:
                    tempo_hub.download_image(name, revision=revision)
                atomic_json(os.path.join(self.base_dir, 'datasets', 'universal_annotations.json'), records)
                dataset = build_dataset(self.base_dir)
            else:
                # Keep local experiments separate from the approved cloud master.
                root = Path(self.base_dir) / 'datasets' / 'local_training'
                image_dir = root / 'datasets' / 'images'
                image_dir.mkdir(parents=True, exist_ok=True)
                records = {}
                import hashlib
                for filename, boxes in self.annotations.items():
                    if not boxes:
                        continue
                    name = hashlib.sha256(os.path.abspath(filename).encode()).hexdigest()[:20] + Path(filename).suffix
                    shutil.copy2(filename, image_dir / name)
                    records[name] = {'status':'approved', 'boxes':boxes}
                atomic_json(str(root / 'datasets' / 'universal_annotations.json'), records)
                dataset = build_dataset(root, origin='local')
            self.compiled_dataset = str(dataset)
            self.progress_signal.emit(50, 'Dataset validated. Starting native model training…')
            self.finished_signal.emit(True, 'Prepared separate training, validation and holdout splits.')
        except Exception as exc:
            self.finished_signal.emit(False, str(exc))

                
class GuidelineIllustration(QLabel):
    """Keep an example's aspect ratio within the available card width."""
    def __init__(self, path, *, source_rect=None, max_width=850):
        super().__init__()
        self._source = QPixmap(path)
        if source_rect is not None and not self._source.isNull():
            self._source = self._source.copy(*source_rect)
        self._max_width = max_width
        self.setMinimumWidth(0)
        self.setMaximumWidth(self._max_width)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.fit_image()

    def fit_image(self):
        if self._source.isNull():
            return
        scaled = self._source.scaledToWidth(max(1, min(self.width(), self._max_width)), Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(scaled)
        if self.minimumHeight() != scaled.height() or self.maximumHeight() != scaled.height():
            self.setFixedHeight(scaled.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_image()


class GuidelinesDialog(QDialog):
    """Annotation instructions and examples for freshly isolated adult mouse cells."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Annotation Guidelines")
        self.resize(1000, 820)

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
            
        header_layout = QVBoxLayout()
        header_sub = QLabel("Left-click to add corners. Right-click to finish an outline. Escape cancels an unfinished outline.")
        header_sub.setWordWrap(True)
        header_layout.addWidget(header_sub)
        layout.addLayout(header_layout)

        completion = QGroupBox('Submission and Reservations')
        completion_layout = QVBoxLayout(completion)
        completion_text = QLabel(
            'Annotate every cell before submitting an image. Save unfinished images as drafts.\n\n'
            'You can edit during review; saved changes sync into your submission. Approval locks the annotations for training. '
            'Maintainers can request corrections before approval.\n\n'
            'Reservations last seven days. Renew before expiry to keep editing. After expiry, another collaborator can adopt the image '
            'and its last synced annotations. Unsynced edits remain only on this computer.')
        completion_text.setWordWrap(True)
        completion_layout.addWidget(completion_text)
        layout.addWidget(completion)

        # Card 1: Boundary Delineation
        card1 = QGroupBox("1. Cell Boundaries")
        card1_layout = QVBoxLayout(card1)
        lbl1 = QLabel(
            "• Follow the visible outer edge of the cell, including its ends.\n"
            "• A phase halo is a bright or dark fringe produced around a cell in phase-contrast microscopy. "
            "Leave this fringe and spaces between cells outside the outline.\n"
            "• Trace one outline per identifiable cell, regardless of how many nuclei it contains.\n"
            "• Use enough polygon points to follow the visible cell boundary."
        )
        lbl1.setWordWrap(True)
        card1_layout.addWidget(lbl1)
        example1 = GuidelineIllustration(os.path.join(os.path.dirname(__file__), 'assets', 'annotation_guidelines', 'boundaries.svg'))
        card1_layout.addWidget(example1)
        layout.addWidget(card1)

        # Card 2: Kissing Junctions & Clustered Cells
        card2 = QGroupBox("2. Touching Cells and Clumps")
        card2_layout = QVBoxLayout(card2)
        lbl2 = QLabel(
            "• Trace each cell separately where the shared boundary is visible.\n"
            "• When individual boundaries cannot be distinguished, trace one outline around the clump.\n"
            "• Label the outline healthy or dead as usual."
        )
        lbl2.setWordWrap(True)
        card2_layout.addWidget(lbl2)
        example2 = GuidelineIllustration(os.path.join(os.path.dirname(__file__), 'assets', 'annotation_guidelines', 'contacts.svg'))
        card2_layout.addWidget(example2)
        layout.addWidget(card2)

        # Card 3: Classification
        card3 = QGroupBox("3. Healthy, Dead, and Nuclei")
        card3_layout = QVBoxLayout(card3)
        lbl3 = QLabel(
            "• Healthy: rod-shaped cells with clear cross-striations and an intact outline.\n"
            "• Dead: rounded, severely shortened cells with lost striations and a disrupted outline.\n"
            "• Leave dust and debris unmarked. Outline confidently identifiable nuclei in purple.\n"
            "• A cell can contain several nuclei. Missing nuclear outlines never mean nuclear absence.\n"
            "• Include all visible cells, including cells assigned to the dead class."
        )
        lbl3.setWordWrap(True)
        card3_layout.addWidget(lbl3)
        example3 = GuidelineIllustration(os.path.join(os.path.dirname(__file__), 'assets', 'annotation_guidelines', 'nuclei.svg'))
        card3_layout.addWidget(example3)
        layout.addWidget(card3)

        references = QGroupBox("4. Published Micrographs")
        reference_layout = QVBoxLayout(references)
        micrograph = os.path.join(os.path.dirname(__file__), 'assets', 'annotation_guidelines',
                                  'adult_mouse_ventricular_micrographs.jpg')
        for caption, rect, width in (
                ('Adult mouse ventricular cardiomyocytes. Scale bar: 50 μm.', (0, 0, 123, 119), 300),
                ('Adult mouse ventricular cardiomyocytes, 1 and 5 hours after isolation. Scale bars: 150 μm.',
                 (0, 263, 226, 123), 550)):
            text = QLabel(caption); text.setWordWrap(True)
            reference_layout.addWidget(text)
            reference_layout.addWidget(GuidelineIllustration(micrograph, source_rect=rect, max_width=width))
        credit = QLabel(
            'Omatsu-Kanbe et al. (2018), Figure 2A and 2E. © 2018 The Authors. '
            '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>. '
            'Cropped panels; original scale bars retained. '
            'Source: NLM/PMC, retrieved 20 October 2026. '
            '<a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC5917088/">Article and full figure</a>.')
        credit.setWordWrap(True); credit.setOpenExternalLinks(True)
        reference_layout.addWidget(credit)
        further = QLabel(
            'More healthy/dead examples: '
            '<a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC3935735/">Fresh adult mouse cells, Figure 5A</a>; '
            '<a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC5091965/">Post-isolation morphology, Figure 3A</a>.')
        further.setWordWrap(True); further.setOpenExternalLinks(True)
        reference_layout.addWidget(further)
        layout.addWidget(references)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_close = QPushButton("Close Guidelines")
        btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)


class AnnotatorTab(QWidget):
    submission_completed = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.annotations = {} # dict of { filepath: [{'class_id': int, 'box': [x,y,w,h]}, ...] }
        self.universal_annotations = {} # For universal mode
        self.current_file = None
        self.current_dir = None
        self.current_rect_items = []
        self.is_universal_mode = False
        self.universal_json_path = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, "universal_annotations.json")
        self.session_file = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, "local_session.json")
        self.lock_manifest = {}
        self.collaborator_id = tempo_hub.get_collaborator_id()
        self._current_file_readonly = False
        self._preview_record = None
        self._preview_name = None

        
        self.class_config = [
            {'name': 'healthy', 'color': QColor(0, 255, 0, 180)},
            {'name': 'dead', 'color': QColor(255, 0, 0, 180)},
            {'name': 'nuclei', 'color': QColor(155, 81, 224, 180)},
        ]
        
        self.setup_ui()
        self._sync_busy = False
        self._lease_sync_busy = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.timeout.connect(self.sync_pending_annotations)
        self.load_universal_annotations()
        self.lock_manifest = tempo_hub.cached_lock_manifest()
        
    def setup_ui(self):
        layout = QHBoxLayout(self)
        # Let resizeEvent stack the panels before their horizontal minimum
        # can force the window wider than the requested width.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        splitter = self.annotation_splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)
        
        # Left: File Navigator & Batch Checkout
        left_panel = self.annotation_navigator = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        
        self.mode_toggle = QComboBox()
        self.mode_toggle.addItems(["Local Images", "Shared Annotation Pool"])
        self.mode_toggle.currentIndexChanged.connect(self.on_mode_changed)
        left_layout.addWidget(QLabel("Mode:"))
        left_layout.addWidget(self.mode_toggle)
        
        self.btn_load = QPushButton("Load Image Directory")
        self.btn_load.clicked.connect(self.load_directory)
        
        self.btn_import = QPushButton("Import Images")
        self.btn_import.clicked.connect(self.import_universal_images)
        self.btn_import.hide()
        
        # Collaboration & SOP Suite
        self.btn_guidelines = QPushButton("Guidelines && Examples")
        self.btn_guidelines.setToolTip("How to outline and label cells, including touching cells and clumps.")
        self.btn_guidelines.clicked.connect(self.open_guidelines_dialog)
        self.btn_guidelines.hide()

        self.lbl_lock_status = PreviewStatusLabel()
        self.lbl_lock_status.setTextFormat(Qt.TextFormat.PlainText)

        left_layout.addWidget(self.btn_load)
        left_layout.addWidget(self.btn_import)
        self.file_error = ErrorDetails()
        left_layout.addWidget(self.file_error)
        self.btn_retry_annotation_save = QPushButton('Retry Saving Annotations')
        self.btn_retry_annotation_save.clicked.connect(self.retry_manual_annotation_save)
        self.btn_retry_annotation_save.hide()
        left_layout.addWidget(self.btn_retry_annotation_save)
        self.annotation_load_error = ErrorDetails()
        left_layout.addWidget(self.annotation_load_error)
        self.btn_retry_annotation_load = QPushButton("Retry Loading Annotations")
        self.btn_retry_annotation_load.clicked.connect(self.retry_annotation_load)
        self.btn_retry_annotation_load.hide()
        left_layout.addWidget(self.btn_retry_annotation_load)
        self.btn_retry_import_save = QPushButton("Retry Saving Imported Images")
        self.btn_retry_import_save.hide()
        self.btn_retry_import_save.clicked.connect(self.retry_import_save)
        left_layout.addWidget(self.btn_retry_import_save)
        
        self.images_heading = QLabel('Images')
        left_layout.addWidget(self.images_heading)
        self.list_files = QListWidget()
        self.list_files.setWordWrap(True)
        selection_key = 'Command' if sys.platform == 'darwin' else 'Ctrl'
        self.list_files.setToolTip(f'{selection_key}-click to select individual images; Shift-click to select a range.')
        self.list_files.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list_files.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_files.currentItemChanged.connect(self.on_file_selected)
        self.list_files.itemSelectionChanged.connect(self.update_submission_readiness)
        self.list_files.itemSelectionChanged.connect(self.update_export_button_state)
        left_layout.addWidget(self.list_files)
        self.shared_browser = SharedImageBrowser(tempo_hub, lambda action, done: run_background(action, done))
        self.shared_browser.can_change_image = self.can_change_annotation_image
        self.shared_browser.preview_ready.connect(self.show_shared_preview)
        self.shared_browser.preview_loading.connect(self.loading_shared_preview)
        self.shared_browser.reserve_requested.connect(self.reserve_shared_images)
        self.shared_browser.open_editable_image = self.open_owned_shared_image
        self.shared_browser.local_previews = self.local_shared_previews
        self.shared_browser.can_reserve_images = lambda: not (
            getattr(self,'_annotation_load_failed',False) or getattr(self,'_annotation_save_unconfirmed',False)
            or getattr(self,'_auto_pending',None))
        self.shared_browser.selection_changed.connect(self.update_submission_readiness)
        self.shared_browser.hide()
        left_layout.addWidget(self.shared_browser, 1)
        self.navigator_scroll = QScrollArea()
        self.navigator_scroll.setWidgetResizable(True)
        self.navigator_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.navigator_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.navigator_scroll.setWidget(left_panel)
        splitter.addWidget(self.navigator_scroll)
        
        # Center: Canvas & Training Overlay
        self.center_stack = QStackedWidget()
        
        self.view = AnnotationView()
        self.view.polygon_drawn.connect(self.add_polygon)
        self.view.mask_selected.connect(self.select_mask_from_canvas)
        self.center_stack.addWidget(self.view)
        self.canvas_hint = QLabel('Load an image folder, then select an image to annotate.')
        self.canvas_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas_hint.setWordWrap(True)
        self.canvas_hint.setStyleSheet("border: 1px solid #465467; border-radius: 3px; padding: 12px;")
        self.center_stack.addWidget(self.canvas_hint)
        self.center_stack.setCurrentWidget(self.canvas_hint)

        canvas_panel = QWidget()
        canvas_layout = QVBoxLayout(canvas_panel)
        canvas_layout.setContentsMargins(4, 0, 4, 0)
        self.lbl_image_name = FilenameLabel()
        self.lbl_image_name.textChanged.connect(self.arrange_preview_header)
        self.lbl_image_name.hide()
        self.preview_header = QWidget()
        self.preview_header.installEventFilter(self)
        preview_header_layout = QHBoxLayout(self.preview_header)
        preview_header_layout.setContentsMargins(0, 0, 0, 0)
        preview_header_layout.setSpacing(12)
        preview_header_layout.addWidget(self.lbl_image_name, 1, Qt.AlignmentFlag.AlignTop)
        preview_header_layout.addWidget(self.lbl_lock_status, 0, Qt.AlignmentFlag.AlignTop)
        canvas_layout.addWidget(self.preview_header)
        canvas_controls = FlowRow()
        for text, action in [('Zoom +', lambda: self.view.zoom_image(1.25)),
                             ('Zoom −', lambda: self.view.zoom_image(.8)),
                             ('Fit', self.view.fit_image)]:
            button = QPushButton(text)
            button.clicked.connect(action)
            canvas_controls.flow.addWidget(button)
        self.show_overlays = QCheckBox('Cells')
        self.show_overlays.setChecked(True)
        self.show_overlays.toggled.connect(self.update_overlay_visibility)
        canvas_controls.flow.addWidget(self.show_overlays)
        contrast = QCheckBox('Enhance contrast')
        contrast.toggled.connect(self.view.set_contrast)
        canvas_controls.flow.addWidget(contrast)
        self.show_nuclei = QCheckBox('Nuclei'); self.show_nuclei.setChecked(True)
        self.show_nuclei.toggled.connect(self.update_overlay_visibility)
        canvas_controls.flow.addWidget(self.show_nuclei)
        canvas_controls.flow.addWidget(InformationButton('Image navigation',
            f'Use the mouse wheel to zoom. Drag with the middle mouse button to pan. Fit restores the complete image view. '
            f'{selection_key}-click a mask to select it; add Shift to add or remove it from the selection. '
            'Repeat at the same spot to cycle through overlapping masks, smallest first. Unfinished drawings are preserved.'))
        canvas_layout.addWidget(canvas_controls)
        canvas_layout.addWidget(self.center_stack, 1)
        self.drawing_notice = InlineNotice()
        self.drawing_notice.setWordWrap(True)
        self.view.drawing_status.connect(self.drawing_notice.setText)
        canvas_layout.addWidget(self.drawing_notice)
        splitter.addWidget(canvas_panel)
        
        # Right: Inspector & Export
        right_panel = self.annotation_inspector = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        # Class Selector
        class_group = QGroupBox("Classes")
        class_layout = QVBoxLayout(class_group)
        self.list_classes = QListWidget()
        self.list_classes.currentRowChanged.connect(self.on_class_changed)
        for i, c in enumerate(self.class_config):
            item = QListWidgetItem(c['name'])
            pixmap = QPixmap(16, 16)
            pixmap.fill(c['color'])
            icon = QIcon(pixmap)
            icon.addPixmap(pixmap, QIcon.Mode.Disabled)
            item.setIcon(icon)
            self.list_classes.addItem(item)
        self.list_classes.setFixedHeight(
            sum(self.list_classes.sizeHintForRow(row) for row in range(self.list_classes.count()))
            + 2 * self.list_classes.frameWidth() + 8)
        self.list_classes.setCurrentRow(0)
        class_layout.addWidget(self.list_classes)
        right_layout.addWidget(class_group)
        
        # Running Category Counts
        counts_group = self.counts_group = QGroupBox('Annotations in Loaded Images')
        counts_layout = QVBoxLayout(counts_group)
        self.count_labels = {}
        for c in self.class_config:
            lbl = QLabel(f"{c['name']}: 0")
            self.count_labels[c['name']] = lbl
            counts_layout.addWidget(lbl)
        self.unclassified_count = QLabel('')
        self.unclassified_count.setWordWrap(True)
        counts_layout.addWidget(self.unclassified_count)
        self.unclassified_count.hide()
        right_layout.addWidget(counts_group)
        
        self.list_boxes = QListWidget()
        self.list_boxes.setMaximumHeight(120)
        self.list_boxes.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_boxes.itemSelectionChanged.connect(self.on_box_selection_changed)
        self.btn_delete = QPushButton("Delete Annotations")
        self.btn_delete.clicked.connect(self.delete_box)
        self.classify_actions = QWidget()
        classify_layout = QHBoxLayout(self.classify_actions)
        classify_layout.setContentsMargins(0, 0, 0, 0)
        classify_layout.setSpacing(6)
        self.btn_mark_healthy = QPushButton('Mark Healthy')
        self.btn_mark_dead = QPushButton('Mark Dead')
        for button, class_id in ((self.btn_mark_healthy, 0), (self.btn_mark_dead, 1)):
            button.clicked.connect(lambda checked=False, cid=class_id: self.classify_selected_shapes(cid))
            button.setEnabled(False)
            classify_layout.addWidget(button)
        classify_layout.addWidget(self.btn_guidelines)

        # Multi-Model Automated Annotation Suite
        self.auto_group = QGroupBox('Model Assistance')
        auto_layout = QVBoxLayout(self.auto_group)

        from tempo_assistance_progress import ModelAssistanceProgress
        self.auto_progress = ModelAssistanceProgress(self.auto_group)
        auto_layout.addWidget(self.auto_progress)

        self.lbl_auto_status = InlineNotice()
        self.lbl_auto_status.setWordWrap(True)
        auto_layout.addWidget(self.lbl_auto_status)
        self.auto_error = ErrorDetails()
        auto_layout.addWidget(self.auto_error)
        self.auto_availability = InlineNotice()
        auto_layout.addWidget(self.auto_availability)

        auto_btn_grid = QGridLayout()
        self.btn_auto_nano = QPushButton("Count Cells")
        set_button_color(self.btn_auto_nano, '#0088CC')
        self.btn_auto_nano.setToolTip("Nano scout: estimate individual-cell counts and detect groups.")
        self.btn_auto_nano.clicked.connect(lambda: self.start_auto_annotation("scout"))

        self.btn_auto_stardist = QPushButton("Suggest Nuclei")
        set_button_color(self.btn_auto_stardist, '#9B51E0')
        self.btn_auto_stardist.setToolTip("StarDist: suggest purple nuclear outlines for review.")
        self.btn_auto_stardist.clicked.connect(lambda: self.start_auto_annotation("stardist"))

        self.btn_auto_cellpose = QPushButton("Outline Cells")
        set_button_color(self.btn_auto_cellpose, '#E67E22')
        self.btn_auto_cellpose.setToolTip("Generate cell masks with Cellpose.")
        self.btn_auto_cellpose.clicked.connect(lambda: self.start_auto_annotation("cellpose"))

        for button in (self.btn_auto_nano, self.btn_auto_stardist, self.btn_auto_cellpose):
            button.setStyleSheet('font-weight: bold;')

        auto_btn_grid.addWidget(self.btn_auto_nano, 0, 0)
        auto_btn_grid.addWidget(self.btn_auto_stardist, 1, 0)
        auto_btn_grid.addWidget(self.btn_auto_cellpose, 2, 0)
        auto_layout.addLayout(auto_btn_grid)
        self.btn_nuclear_edit = QPushButton('Annotate Nuclei')
        self.btn_nuclear_edit.clicked.connect(self.begin_nuclear_edit)
        auto_layout.addWidget(self.btn_nuclear_edit)
        self.btn_nuclear_submit = QPushButton('Submit Nuclei')
        self.btn_nuclear_submit.clicked.connect(self.submit_nuclear_layer)
        self.btn_nuclear_submit.hide()
        auto_layout.addWidget(self.btn_nuclear_submit)
        self.btn_restore_annotations = QPushButton('Restore Backup')
        self.btn_restore_annotations.setToolTip('Restore annotations from a saved backup.')
        self.btn_restore_annotations.clicked.connect(self.restore_annotation_backup)

        # Backwards-compatibility alias
        self.btn_auto_annotate = self.btn_auto_cellpose

        self.submission_group = QGroupBox("Submit for Review")
        submission_layout = QVBoxLayout(self.submission_group)
        submission_layout.setSpacing(6)
        submission_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        self.lbl_readiness_badge = QLabel("Select completed images to submit.")
        self.lbl_readiness_badge.setWordWrap(True)
        self.lbl_readiness_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_readiness_badge.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        submission_layout.addWidget(self.lbl_readiness_badge)
        self.btn_publish = QPushButton("Submit Selected")
        self.btn_publish.setEnabled(False)
        self.lbl_readiness_badge.setToolTip('')
        self.btn_publish.setToolTip("Submit images only after annotating every cell.")
        self.btn_publish.clicked.connect(self.publish_submission)
        submission_layout.addWidget(self.btn_publish)
        self.reservation_actions = QWidget()
        reservation_layout = QHBoxLayout(self.reservation_actions)
        reservation_layout.setContentsMargins(0, 0, 0, 0)
        reservation_layout.setSpacing(6)
        self.btn_renew = QPushButton('Renew (7 Days)')
        self.btn_renew.clicked.connect(self.renew_current_lease)
        self.btn_renew.hide()
        self.btn_release = QPushButton('Release Reservation')
        self.btn_release.setToolTip('Make this image available to others. Saved annotations are retained.')
        self.btn_release.clicked.connect(self.release_current_lease)
        self.btn_release.hide()
        reservation_layout.addWidget(self.btn_renew)
        reservation_layout.addWidget(self.btn_release)
        submission_layout.addWidget(self.reservation_actions)
        self.reservation_actions.hide()
        self.submission_status = InlineNotice()
        self.submission_status.setWordWrap(True)
        submission_layout.addWidget(self.submission_status)
        self.lbl_sync_status = InlineNotice()
        self.lbl_sync_status.setWordWrap(True)
        submission_layout.addWidget(self.lbl_sync_status)
        self.submission_error = ErrorDetails()
        submission_layout.addWidget(self.submission_error)
        self.sync_error = ErrorDetails()
        submission_layout.addWidget(self.sync_error)
        self.reservation_error = ErrorDetails()
        submission_layout.addWidget(self.reservation_error)
        self.reservation_refresh_error = ErrorDetails()
        submission_layout.addWidget(self.reservation_refresh_error)
        self.adoption_notice = InlineNotice()
        self.adoption_notice.setWordWrap(True)
        submission_layout.addWidget(self.adoption_notice)
        self.adoption_error = ErrorDetails()
        submission_layout.addWidget(self.adoption_error)
        self.identity_error = ErrorDetails()
        submission_layout.addWidget(self.identity_error)
        self.lease_timer = QTimer(self)
        self.lease_timer.timeout.connect(self.enforce_local_lease)
        self.lease_timer.start(30000)

        self.btn_export = QPushButton("Export Selected Images")
        set_button_color(self.btn_export, '#AA00FF')
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export_annotations)
        
        right_layout.addWidget(QLabel("Annotations"))
        right_layout.addWidget(self.list_boxes)
        right_layout.addWidget(self.classify_actions)
        right_layout.addWidget(self.btn_delete)
        right_layout.addWidget(self.auto_group)
        right_layout.addWidget(self.btn_restore_annotations)
        self.restore_feedback = QWidget()
        restore_layout = QVBoxLayout(self.restore_feedback)
        restore_layout.setContentsMargins(0, 0, 0, 0)
        self.restore_status = QLabel('')
        self.restore_status.setWordWrap(True)
        restore_layout.addWidget(self.restore_status)
        self.restore_error = ErrorDetails()
        restore_layout.addWidget(self.restore_error)
        right_layout.addWidget(self.restore_feedback)
        self.restore_feedback.hide()
        self.submission_group.hide()
        
        right_layout.addStretch()
        self.export_group = QWidget()
        export_layout = QVBoxLayout(self.export_group)
        export_layout.setContentsMargins(0, 0, 0, 0)
        export_layout.addWidget(self.btn_export)
        self.export_status = QLabel('')
        self.export_status.setWordWrap(True)
        export_layout.addWidget(self.export_status)
        self.export_error = ErrorDetails()
        export_layout.addWidget(self.export_error)
        right_layout.addWidget(self.export_group)
        inspector = self.annotation_inspector_scroll = QScrollArea()
        inspector.setWidgetResizable(True)
        inspector.setMinimumWidth(310)
        inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector.setWidget(right_panel)
        sidebar = self.annotation_sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(inspector, 1)
        self.submission_scroll = QScrollArea()
        self.submission_scroll.setWidgetResizable(True)
        self.submission_scroll.setMaximumHeight(360)
        self.submission_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.submission_scroll.setWidget(self.submission_group)
        self.submission_scroll.hide()
        sidebar_layout.addWidget(self.submission_scroll)
        splitter.addWidget(sidebar)

        splitter.setSizes([250, 450, 330])
        self.update_running_counts()
        self.set_auto_buttons_enabled(False)
        self.btn_delete.setEnabled(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange_annotation_panels()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.LayoutRequest, QEvent.Type.FontChange):
            self.arrange_annotation_panels()
        return result

    def arrange_annotation_panels(self):
        if not hasattr(self, 'annotation_sidebar'):
            return
        self.arrange_preview_header()
        for row in (self.classify_actions, self.reservation_actions):
            for index in range(row.layout().count()):
                button = row.layout().itemAt(index).widget()
                natural_width = button.sizeHint().width()
                if row.layout().stretch(index) != natural_width:
                    row.layout().setStretch(index, natural_width)
        self.list_classes.setFixedHeight(
            sum(self.list_classes.sizeHintForRow(row) for row in range(self.list_classes.count()))
            + 2 * self.list_classes.frameWidth() + 8)
        self.annotation_inspector_scroll.setMinimumHeight(
            max(self.classify_actions.minimumSizeHint().height(), self.btn_delete.sizeHint().height()) + 24)
        # Keep the image beside its controls when their actual text fits. Large
        # fonts need full-width, independently scrollable panels instead of
        # squeezing buttons and labels below their text bounds.
        navigation = self.annotation_navigator.minimumSizeHint().width()
        inspector = max(self.annotation_inspector.minimumSizeHint().width(),
                        self.submission_group.minimumSizeHint().width(), 310)
        from PyQt6.QtWidgets import QStyle
        scrollbar = max(self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent),
                        self.navigator_scroll.verticalScrollBar().sizeHint().width(),
                        self.annotation_inspector_scroll.verticalScrollBar().sizeHint().width())
        self.navigator_scroll.setMinimumWidth(navigation + scrollbar)
        self.annotation_sidebar.setMinimumWidth(inspector + scrollbar)
        submission_height = self.submission_group.layout().totalHeightForWidth(
            max(100, self.submission_scroll.viewport().width()))
        self.submission_scroll.setMinimumHeight(min(360, max(0, submission_height)))
        self.annotation_sidebar.setMinimumHeight(self.annotation_sidebar.minimumSizeHint().height())
        canvas_width = max(240,self.center_stack.parentWidget().minimumSizeHint().width())
        minimum = navigation + inspector + canvas_width + 2*scrollbar + 40
        orientation = (Qt.Orientation.Horizontal if self.width() >= minimum
                       else Qt.Orientation.Vertical)
        if self.annotation_splitter.orientation() != orientation:
            self.annotation_splitter.setOrientation(orientation)
            self.annotation_splitter.setSizes([250, 450, 330])

    def arrange_preview_header(self):
        label = self.lbl_image_name
        # Short filenames should not force an otherwise fitting status to wrap.
        natural = label.fontMetrics().horizontalAdvance(label.toPlainText()) + 12
        label.setMaximumWidth(max(80, natural))
        if hasattr(self, 'preview_header'):
            status = self.lbl_lock_status
            available = max(0, self.preview_header.width() - 12)
            # When both need wrapping, give the filename useful room too.
            status.setMaximumWidth(min(status.sizeHint().width() + 2,
                                       max(0, available - min(natural, available // 2))))

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'preview_header', None) and event.type() == QEvent.Type.Resize:
            self.arrange_preview_header()
        return super().eventFilter(watched, event)
        
    def update_canvas_hint(self):
        if self.is_universal_mode and getattr(self, '_annotation_load_failed', False):
            text = 'Annotation loading failed. Use Retry Loading Annotations after correcting the file error.'
        elif self.is_universal_mode:
            text = 'Select an image to preview it. Reserve Image enables annotation editing.'
        else:
            text = 'Load an image folder, then select an image to annotate.'
        self.canvas_hint.setText(text)

    def on_mode_changed(self, index):
        if not self.can_change_annotation_image():
            self.mode_toggle.blockSignals(True)
            self.mode_toggle.setCurrentIndex(1 if self.is_universal_mode else 0)
            self.mode_toggle.blockSignals(False)
            return
        self.clear_detection_feedback()
        self.submission_status.clear()
        self.lbl_lock_status.clear()
        self.shared_browser.invalidate()
        self.shared_browser.hide()
        self.list_files.show()
        self.images_heading.show()
        self._preview_record = None
        self._preview_name = None
        self.counts_group.setTitle('Annotations in Loaded Images')
        self.update_running_counts()
        self.refresh_account_identity()
        self.is_universal_mode = (index == 1)
        self.update_canvas_hint()
        if self.is_universal_mode:
            self.btn_load.hide()
            self.btn_import.show()
            self.btn_guidelines.show()
            self.btn_export.hide()
            self.export_group.hide()
            self.submission_group.show()
            self.submission_scroll.show()
            self.current_dir = tempo_backend.UNIVERSAL_DATASET_DIR
            self.list_files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self.refresh_universal_file_list()
        else:
            self.btn_load.show()
            self.btn_import.hide()
            self.btn_guidelines.hide()
            self.list_files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self.submission_group.hide()
            self.submission_scroll.hide()
            self.btn_export.show()
            self.export_group.show()
            self.current_dir = None
            self.list_files.clear()
            self.annotations = {}
            self.lbl_lock_status.setText("")
        
        self.current_file = None
        self.lbl_image_name.clear()
        self.lbl_image_name.hide()
        self.view._cleanup_drawing()
        self.view.set_mask_items([])
        self.view.scene.clear()
        self.view.image_item = None
        self.center_stack.setCurrentWidget(self.canvas_hint)
        self.set_auto_buttons_enabled(False)
        self.refresh_boxes()
        self.update_running_counts()
        self.update_export_button_state()
        self.update_submission_readiness()
        self.arrange_annotation_panels()
        if self.is_universal_mode:
            self.open_checkout_dialog()
        
    def refresh_account_identity(self):
        identity = tempo_hub.get_collaborator_id()
        if identity == self.collaborator_id:
            return
        self.identity_error.clear()
        # Keep draft authorship independent of the account currently using the GUI.
        for draft in self.universal_annotations.values():
            draft.setdefault('draft_owner', self.collaborator_id)
        save_error = None
        try:
            self.save_universal_annotations()
        except OSError as exc:
            save_error = exc
        self.collaborator_id = identity
        self.shared_browser.invalidate()
        self._preview_record = None
        self._preview_name = None
        if self.current_file is None:
            self.on_file_selected(None)
        self._apply_current_lease()
        self.refresh_lease_status()
        if self.shared_browser.isVisible():
            self.shared_browser.load()
        if save_error:
            self.identity_error.set_error('The account changed, but local draft ownership could not be saved. Check local storage before editing or closing TEMPO.', save_error)

    def refresh_universal_file_list(self, refresh=True):
        selected_paths = {item.data(Qt.ItemDataRole.UserRole) for item in self.list_files.selectedItems()}
        current_path = self.current_file

        images_manifest = self.lock_manifest.get("images", {})
        archived = getattr(self, '_pending_archive_paths', [])
        self._pending_archive_paths = archived
        self.adoption_error.clear()
        try:
            for name, draft in self.universal_annotations.items():
                rec = images_manifest.get(name, {})
                if draft.get('draft_owner', self.collaborator_id) == self.collaborator_id and draft.get('boxes') and draft.get('status') != 'approved' and rec.get('claimed_by') not in (None, '', self.collaborator_id) and not draft.get('orphan_backup'):
                    backup = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, f'orphaned_backup_{time.time_ns()}.json')
                    atomic_json(backup, {name:draft})
                    draft['orphan_backup'] = backup
                    draft['status'] = 'expired'
                    archived.append(os.path.basename(backup))
            if archived:
                self.save_universal_annotations()
                self.adoption_notice.setText(f"{len(archived)} local annotation {'backup' if len(archived) == 1 else 'backups'} saved after reservations changed.")
                self.adoption_notice.setToolTip('\n'.join(archived))
                self._pending_archive_paths = []
        except (OSError, ValueError) as exc:
            self.adoption_error.set_error('Could not finish archiving local annotations after the reservation changed. Your current drafts remain loaded. Check local storage before refreshing again.', exc)
            self._apply_current_lease()
            return
        self.list_files.blockSignals(True)
        self.list_files.clear()
        my_id = self.collaborator_id
        now = datetime.datetime.now(datetime.timezone.utc)

        for filename, data in sorted(self.universal_annotations.items(), key=lambda entry: [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', entry[1].get('original_filename') or entry[0])]):
            filepath = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, filename)
            if not os.path.exists(filepath):
                continue

            rec = images_manifest.get(filename, {})
            status = rec.get("status", data.get("status", "available"))
            owner = rec.get("claimed_by", "")

            # Check expiration if locked
            if status in ('locked', 'submitted') and rec.get("expires_at"):
                try:
                    exp_dt = datetime.datetime.fromisoformat(rec["expires_at"])
                    if now > exp_dt:
                        status = "orphaned"
                except Exception:
                    pass

            boxes_count = len(data.get("boxes", []))
            item = QListWidgetItem(data.get('original_filename') or filename)
            readable_status = {'approved': 'Approved', 'submitted': 'Awaiting review',
                               'locked': 'Reserved', 'orphaned': 'Available to adopt',
                               'available': 'Available'}.get(status, status)
            if status == 'locked':
                readable_status = ('Reserved by you' if owner == self.collaborator_id
                                   else 'Reserved by another account')
            item.setText(f"{data.get('original_filename') or filename}\n{readable_status} · {boxes_count} {'annotation' if boxes_count == 1 else 'annotations'}")
            item.setToolTip(f"{data.get('original_filename') or filename}\n{readable_status} · {boxes_count} {'annotation' if boxes_count == 1 else 'annotations'}")
            item.setData(Qt.ItemDataRole.UserRole, filepath)
            item.setData(Qt.ItemDataRole.UserRole + 1, status)
            item.setData(Qt.ItemDataRole.UserRole + 2, owner)
            self.list_files.addItem(item)
            item.setSelected(filepath in selected_paths)
            if filepath == current_path:
                self.list_files.setCurrentItem(item, QItemSelectionModel.SelectionFlag.NoUpdate)

        self.list_files.blockSignals(False)

        self.update_submission_readiness()
        if any(r.get('sync_pending') for r in self.universal_annotations.values()):
            self._sync_timer.start(1000)
        if refresh:
            self.refresh_lease_status()

    def refresh_lease_status(self):
        if self._lease_sync_busy:
            return
        self._lease_sync_busy = True
        revision = getattr(self, '_reservation_revision', 0)
        def ready(manifest, error):
            if revision != getattr(self, '_reservation_revision', 0):
                self._lease_sync_busy = False
                self.refresh_lease_status()
                return
            self.on_lease_status_finished(manifest, error)
        run_background(lambda: tempo_hub.fetch_lock_manifest(force_refresh=True), ready)

    def on_lease_status_finished(self, manifest, error):
        self._lease_sync_busy = False
        if error:
            self.reservation_refresh_error.set_error('Reservations could not be refreshed. Showing saved status; another check will run automatically.', error)
            return
        self.reservation_refresh_error.clear()
        self.lock_manifest = manifest
        if self._annotation_load_failed:
            self._apply_current_lease()
            return
        previous = self.universal_annotations
        try:
            normalized = merge_image_records(self.universal_annotations, manifest)
            if normalized != self.universal_annotations:
                backup = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR,
                                      f'identity_backup_{time.time_ns()}.json')
                atomic_json(backup, self.universal_annotations)
                self.universal_annotations = normalized
                self.save_universal_annotations()
        except (OSError, ValueError) as exc:
            self.universal_annotations = previous
            self.reservation_refresh_error.set_error('Image identities could not be reconciled. '
                'Your existing annotations were preserved. Resolve the conflict before editing shared images.', exc)
            self._annotation_load_failed = True
            self._apply_current_lease()
            return
        self.refresh_account_identity()
        if self.is_universal_mode:
            self.refresh_universal_file_list(refresh=False)
            if self.current_file:
                self._apply_current_lease()
            catalog = self.shared_browser.catalog
            if (not self.shared_browser.isHidden() and catalog is not None
                    and catalog['manifest'].get('images',{}) != manifest.get('images',{})):
                self.shared_browser.load()

    def import_universal_images(self):
        if not self.btn_retry_import_save.isHidden():
            self.retry_import_save()
            if not self.btn_retry_import_save.isHidden():
                return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Images to Import", "", 
            "Images (*.png *.jpg *.jpeg *.tif *.tiff)",
        )
        if not files: return
        
        dialog = UniversalImportDialog(self)
        if not dialog.exec():
            return
        import uuid
        self.file_error.clear()
        mag, well = dialog.get_data()
        imported = []
        failures = []
        for source in files:
            original_name = os.path.basename(source)
            filename = f"{uuid.uuid4().hex}_{original_name}"
            destination = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, filename)
            created = False
            try:
                with open(source, 'rb') as incoming, open(destination, 'xb') as outgoing:
                    created = True
                    shutil.copyfileobj(incoming, outgoing)
                self.universal_annotations[filename] = {
                    "original_filename": original_name,
                    "magnification": mag,
                    "well_number": well,
                    "boxes": [],
                    "trained": False,
                }
                imported.append(filename)
            except OSError as exc:
                failures.append(f'{original_name}: {exc}')
                if created:
                    try:
                        os.remove(destination)
                    except OSError as cleanup_error:
                        failures.append(f'Could not remove incomplete copy {filename}: {cleanup_error}')
        self._import_copy_failures = failures.copy()
        if imported:
            try:
                self.save_universal_annotations()
            except OSError as exc:
                self.btn_retry_import_save.show()
                failures.append(f'Image details could not be saved: {exc}')
                self.file_error.set_error(
                    'Images were copied, but their details could not be saved. Keep this window open and check storage permissions and available space, then choose Retry Saving Imported Images.',
                    '\n'.join(failures))
                return
            self.refresh_universal_file_list()
        if failures:
            self.file_error.set_error(
                f'{len(imported)} of {len(files)} images imported. Check the failed files and import them again.',
                '\n'.join(failures))

    def retry_import_save(self):
        try:
            self.save_universal_annotations()
        except OSError as exc:
            self.file_error.set_error('Could not save imported image details. Check storage permissions and available space, then retry.', exc)
            return
        self.btn_retry_import_save.hide()
        self.file_error.clear()
        self.refresh_universal_file_list()
        failures = getattr(self, '_import_copy_failures', [])
        if failures:
            self.file_error.set_error('Imported image details were saved. Some files could not be copied; check the details and import those files again.', '\n'.join(failures))

    def load_universal_annotations(self):
        if getattr(self, '_annotation_save_unconfirmed', False):
            return False
        records = {}
        session = {}
        try:
            for path in (self.universal_json_path, self.session_file):
                try:
                    with open(path, encoding='utf-8') as stream:
                        data = json.load(stream)
                except FileNotFoundError:
                    continue
                if not isinstance(data, dict):
                    raise ValueError(f'{os.path.basename(path)}: expected an annotation object.')
                incoming = data.get('annotations', {}) if path == self.session_file else data
                if path == self.session_file:
                    if data.get('dataset_repo') not in (None, tempo_hub.get_dataset_repo()):
                        raise ValueError('These saved annotations belong to a different shared dataset.')
                    session = copy.deepcopy(data)
                if not isinstance(incoming, dict) or any(
                        not isinstance(record, dict) or not isinstance(record.get('boxes', []), list)
                        for record in incoming.values()):
                    raise ValueError(f'{os.path.basename(path)}: invalid annotation records.')
                records.update(incoming)
            converted = {}
            records = merge_image_records(records, getattr(self, 'lock_manifest', {})
                                           or tempo_hub.cached_lock_manifest())
            for name, record in records.items():
                converted_record = pixel_record(record)
                converted_record['boxes'] = self.read_annotation_shapes(name, converted_record.get('boxes', []))
                converted[name] = converted_record
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self._annotation_load_failed = True
            self.update_canvas_hint()
            self.annotation_load_error.set_error('Shared annotations could not be loaded. Saving is paused to protect existing files. Restore the damaged file or correct its permissions, then retry loading.', exc)
            self.btn_retry_annotation_load.show()
            self.btn_import.setEnabled(False)
            self._apply_current_lease()
            self.update_submission_readiness()
            return False
        self.universal_annotations = converted
        self._session_revision = session.get('session_revision', 0)
        self._session_base = session
        self._annotation_load_failed = False
        self.update_canvas_hint()
        self.annotation_load_error.clear()
        self.btn_retry_annotation_load.hide()
        self.btn_import.setEnabled(True)
        return True

    def retry_annotation_load(self):
        recovery = None
        unconfirmed = getattr(self, '_annotation_save_unconfirmed', False)
        if getattr(self, '_annotation_session_conflict', False):
            recovery = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR,
                                    f'auto_annotation_backup_{time.time_ns()}.json')
            try:
                atomic_bytes(recovery, json.dumps(self.universal_annotations, indent=2, allow_nan=False).encode('utf-8'))
            except (OSError, ValueError, TypeError) as error:
                self.annotation_load_error.set_error(
                    'Your current edits could not be backed up. Check storage permissions and free space, then retry Reload Saved Annotations.', error)
                return
            self._annotation_save_unconfirmed = False
        if self.load_universal_annotations():
            self._annotation_session_conflict = False
            self.btn_retry_annotation_load.setText('Retry Loading Annotations')
            if recovery is not None:
                self.btn_retry_annotation_save.hide()
                self.file_error.clear()
                self.reservation_error.clear()
            self.refresh_universal_file_list()
            self._apply_current_lease()
            if recovery is not None:
                self.restore_feedback.show()
                self.restore_error.clear()
                self.restore_status.setText('Saved annotations reloaded. Earlier edits were kept in '
                                            + os.path.basename(recovery) + '. Use Restore Backup to recover them.')
        elif recovery is not None:
            self._annotation_save_unconfirmed = unconfirmed

    def _session_annotation_record(self, name, record):
        converted = pixel_record(record)
        converted['boxes'] = self.read_annotation_shapes(name, converted.get('boxes', []))
        for shape in converted['boxes']:
            if not shape.get('polygon') and 'box' in shape:
                x, y, width, height = shape['box']
                shape['polygon'] = [[x,y], [x+width,y], [x+width,y+height], [x,y+height]]
            if shape.get('polygon'):
                shape.pop('box', None)
        return converted

    def save_universal_annotations(self):
        if getattr(self, '_annotation_load_failed', False):
            raise OSError('Shared annotation loading failed; retry loading before saving.')
        visible_name = (os.path.basename(self.current_file) if self.current_file and self.is_universal_mode
                        and self._preview_record is None else None)
        previous_boxes = (self._session_annotation_record(visible_name, self.universal_annotations.get(visible_name, {}))['boxes']
                          if visible_name else None)
        try:
            updated = update_session(self.session_file, lambda current: {
                **current,
                'collaborator_id': self.collaborator_id,
                'dataset_repo': tempo_hub.get_dataset_repo(),
                'last_updated': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'claimed_images': list(dict.fromkeys(
                    [name for name in current.get('claimed_images', []) if name not in self.lock_manifest.get('images', {})]
                    + [name for name,rec in self.lock_manifest.get('images', {}).items() if rec.get('claimed_by') == self.collaborator_id])),
                'annotations': self.universal_annotations,
            }, expected_revision=getattr(self, '_session_revision', 0), annotations_path=self.universal_json_path,
                base_session=getattr(self, '_session_base', None), normalize_record=self._session_annotation_record)
        except AnnotationSessionConflict as error:
            self._annotation_session_conflict = True
            self._annotation_load_failed = True
            self._sync_timer.stop()
            self.btn_retry_annotation_load.setText('Reload Saved Annotations')
            self.btn_retry_annotation_load.show()
            self.annotation_load_error.set_error(
                'Saved annotations changed in another operation. Close other TEMPO windows, then choose Reload Saved Annotations. TEMPO will first keep a backup of your current edits.', error)
            self.btn_import.setEnabled(False)
            self._apply_current_lease()
            self.annotation_navigator.layout().activate()
            self.navigator_scroll.ensureWidgetVisible(self.btn_retry_annotation_load)
            raise
        self._session_revision = updated['session_revision']
        self._session_base = copy.deepcopy(updated)
        merged = {name:self._session_annotation_record(name, record)
                  for name,record in updated['annotations'].items()}
        for name in list(self.universal_annotations):
            if name not in merged:
                del self.universal_annotations[name]
        for name, record in merged.items():
            if self.universal_annotations.get(name) != record:
                self.universal_annotations[name] = record
        if visible_name and previous_boxes != self.universal_annotations.get(visible_name, {}).get('boxes', []):
            self.refresh_boxes()
            self.update_running_counts()
            self.update_file_status()
            self.update_submission_readiness()
        if hasattr(self, 'identity_error'):
            self.identity_error.clear()

    def normalized_draft(self, name):
        image = QImage(os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, name))
        if image.isNull():
            raise ValueError(f'Cannot read {name}.')
        record = normalize_record(self.universal_annotations[name], image.width(), image.height())
        record.pop('sync_pending', None)
        record.pop('orphan_backup', None)
        record.pop('draft_owner', None)
        return record

    def sync_pending_annotations(self):
        if getattr(self,'_nuclear_only',False):
            self.sync_nuclear_layer()
            return
        if getattr(self, '_annotation_save_unconfirmed', False):
            return
        if getattr(self, '_shutdown_requested', False):
            return
        if getattr(self, '_annotation_load_failed', False):
            return
        if self._sync_busy:
            return
        try:
            payload = {name:self.normalized_draft(name) for name,record in self.universal_annotations.items()
                       if record.get('sync_pending') and record.get('draft_owner', self.collaborator_id) == self.collaborator_id and tempo_hub._owned(self.lock_manifest.get('images', {}).get(name, {}), self.collaborator_id)}
            if not payload:
                return
            self._sync_snapshot = payload
            self._sync_busy = True
            self.sync_error.clear()
            self.lbl_sync_status.setText('Syncing annotations…')
            user = self.collaborator_id
            run_background(lambda: tempo_hub.sync_annotation_drafts(user, payload), self.on_annotation_sync_finished)
        except Exception as exc:
            self._sync_busy = False
            self.lbl_sync_status.setText('Annotations pending sync')
            self.sync_error.set_error('The draft could not be prepared for sync. Review the details and correct the affected image before saving again.', exc)

    def on_annotation_sync_finished(self, result, error):
        self._sync_busy = False
        if error:
            self.lbl_sync_status.setText('Sync failed · retrying in 30 seconds')
            self.sync_error.set_error('Check your connection and reservation status. Your unsynced draft remains saved locally.', error)
            self._sync_timer.start(30000)
            return
        self.sync_error.clear()
        cleared = []
        try:
            for name, sent in self._sync_snapshot.items():
                if name in self.universal_annotations and self.normalized_draft(name) == sent:
                    if self.universal_annotations[name].pop('sync_pending', None):
                        cleared.append(name)
            self.save_universal_annotations()
        except Exception as exc:
            for name in cleared:
                self.universal_annotations[name]['sync_pending'] = True
            self.lbl_sync_status.setText('Cloud sync succeeded · local confirmation was not saved')
            self.sync_error.set_error('Check local storage permissions and available space. TEMPO will retry syncing and saving the confirmation in 30 seconds.', exc)
            self._sync_timer.start(30000)
            return
        pending = any(r.get('sync_pending') for r in self.universal_annotations.values())
        self.lbl_sync_status.setText('Annotations pending sync' if pending else '')
        self.submission_status.clear()
        if pending:
            self._sync_timer.start(1000)

    def load_directory(self):
        if not self.can_change_annotation_image():
            return
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Raw Images Directory", tempo_backend.RAW_DIR,
        )
        if not dir_path: return
        self.file_error.clear()
        try:
            filenames = sorted(os.listdir(dir_path), key=lambda name: [int(part) if part.isdigit() else part.lower() for part in re.split(r'(\d+)', name)])
        except OSError as exc:
            self.file_error.set_error('Could not open this image folder. Check its permissions or choose another folder. The current images remain loaded.', exc)
            return
        try:
            annotations = self.read_local_annotations(dir_path)
        except (OSError, ValueError, TypeError) as exc:
            self.file_error.set_error('Could not load annotations.json from this folder. The current images remain loaded. Check the file or restore a valid backup before opening this folder.', exc)
            return
        self.current_dir = dir_path
        self.annotations = annotations
        self.list_files.clear()
        for f in filenames:
            if f.lower().endswith(('.tif', '.png', '.jpg', '.jpeg')):
                item = QListWidgetItem(f)
                item.setData(Qt.ItemDataRole.UserRole, os.path.join(dir_path, f))
                self.list_files.addItem(item)
        self.update_file_status()
        self.update_running_counts()
        self.update_export_button_state()

    def read_annotation_shapes(self, filename, boxes):
        import math
        if not filename or os.path.basename(filename) != filename or '\\' in filename or filename in ('.', '..'):
            raise ValueError('Invalid image filename in annotations.')
        shapes = []
        if not isinstance(boxes, list):
            raise ValueError(f'{filename}: expected a list of shapes.')
        boxes = migrate_record({'boxes':[
            {'class_id': 0, 'box': box} if isinstance(box, list) else box
            for box in boxes]})['boxes']
        for box in boxes:
            if isinstance(box, list):
                box = {'class_id': 0, 'box': box}
            if not isinstance(box, dict) or type(box.get('class_id')) is not int or not 0 <= box['class_id'] < len(self.class_config):
                raise ValueError(f'{filename}: invalid shape class.')
            points = box.get('polygon')
            if points:
                valid = isinstance(points, list) and len(points) >= 3 and all(
                    isinstance(point, (list, tuple)) and len(point) == 2
                    and all(isinstance(value, (int, float)) and math.isfinite(value) for value in point)
                    for point in points)
            else:
                bounds = box.get('box')
                valid = isinstance(bounds, (list, tuple)) and len(bounds) == 4 and all(
                    isinstance(value, (int, float)) and math.isfinite(value) for value in bounds)
            if not valid:
                raise ValueError(f'{filename}: invalid shape coordinates.')
            shapes.append(box)
        return shapes

    def read_local_annotations(self, directory):
        json_path = os.path.join(directory, "annotations.json")
        try:
            with open(json_path, encoding='utf-8') as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError('Annotations must map image filenames to lists of shapes.')
        if 'format' in data:
            if data.get('format') != 'tempo-local-annotations-v3' or not isinstance(data.get('images'),dict):
                raise ValueError('Update TEMPO to read this annotation format.')
            data = data['images']
        annotations = {}
        for filename, boxes in data.items():
            if not isinstance(boxes, list):
                raise ValueError(f'{filename}: expected a list of shapes.')
            shapes = self.read_annotation_shapes(filename, boxes)
            annotations[os.path.join(directory, filename)] = shapes
        return annotations

    def save_annotations_to_disk(self):
        if getattr(self,'_nuclear_only',False):
            name = os.path.basename(self.current_file)
            self.universal_annotations[name]['nuclear_sync_pending'] = True
            self.save_universal_annotations()
            self.lbl_sync_status.setText('Nuclear edits pending sync')
            self._sync_timer.start(1000)
            return
        if self.is_universal_mode:
            if self.current_file and not self._current_file_readonly:
                name = os.path.basename(self.current_file)
                self.universal_annotations[name]['sync_pending'] = True
                self.universal_annotations[name]['draft_owner'] = self.collaborator_id
            self.save_universal_annotations()
            self.lbl_sync_status.setText('Annotations pending sync')
            self._sync_timer.start(1000)
        else:
            if not self.current_dir: return
            json_path = os.path.join(self.current_dir, "annotations.json")
            data = {os.path.basename(filepath): boxes for filepath, boxes in self.annotations.items()}
            atomic_json(json_path, {'format':'tempo-local-annotations-v3', 'images':data})

    def can_change_annotation_image(self):
        if getattr(self,'_nuclear_busy',False) or (getattr(self,'_nuclear_only',False) and self.universal_annotations.get(os.path.basename(self.current_file or ''),{}).get('nuclear_sync_pending')):
            self.lbl_sync_status.setText('Sync nuclear edits before changing images.')
            return False
        if getattr(self, '_annotation_save_unconfirmed', False):
            if self.file_error.isHidden():
                self.file_error.set_error('Save the current annotations before changing images.',
                                          OSError('The latest annotation save has not been confirmed.'))
            return False
        return not bool(getattr(self, '_auto_pending', None))

    def on_file_selected(self, item, previous=None):
        self.clear_scout_result()
        if not self.can_change_annotation_image():
            self.list_files.blockSignals(True)
            self.list_files.setCurrentItem(previous)
            self.list_files.blockSignals(False)
            return
        self._nuclear_only = False
        self._nuclear_expected = None
        self.btn_nuclear_submit.hide()
        self.list_classes.setEnabled(True)
        next_file = item.data(Qt.ItemDataRole.UserRole) if item else None
        if next_file != self.current_file or self._preview_name is not None or item is None:
            self.clear_detection_feedback()
            self.submission_status.clear()
        self._preview_record = None
        self._preview_name = None
        self.counts_group.setTitle('Annotations in Loaded Images')
        self.update_running_counts()
        if not item:
            self.current_file = None
            self.lbl_image_name.clear()
            self.lbl_image_name.hide()
            self.view._cleanup_drawing()
            self.view.set_mask_items([])
            self.view.scene.clear()
            self.view.image_item = None
            self.current_rect_items = []
            self.list_boxes.clear()
            self.update_canvas_hint()
            self.center_stack.setCurrentWidget(self.canvas_hint)
            self.set_auto_buttons_enabled(False)
            self.btn_delete.setEnabled(False)
            self.btn_renew.hide()
            self.btn_release.hide()
            self.reservation_actions.hide()
            self.lbl_lock_status.clear()
            return
        self.current_file = item.data(Qt.ItemDataRole.UserRole)
        name = os.path.basename(self.current_file)
        display_name = self.universal_annotations.get(name, {}).get('original_filename') if self.is_universal_mode else None
        self.lbl_image_name.setText(display_name or name)
        self.lbl_image_name.show()
        self.view.set_image(self.current_file)
        self.center_stack.setCurrentWidget(self.view)
        self._current_file_readonly = False
        self.btn_renew.hide()
        self.btn_release.hide()
        if self.is_universal_mode:
            filename = os.path.basename(self.current_file)
            record = self.lock_manifest.get('images', {}).get(filename, {})
            self._current_file_readonly = getattr(self, '_annotation_load_failed', False) or not tempo_hub._owned(record, self.collaborator_id) or self.has_foreign_pending_draft(filename)
            status = record.get('status', 'available')
            self.btn_renew.setVisible(status in ('locked', 'submitted') and record.get('claimed_by') == self.collaborator_id)
            self.btn_release.setVisible(status == 'locked' and record.get('claimed_by') == self.collaborator_id)
            self.update_reservation_status(record)
        else:
            self.lbl_lock_status.setText('')
        self.btn_delete.setEnabled(not self._current_file_readonly)
        self.set_auto_buttons_enabled(not self._current_file_readonly)
        if self._current_file_readonly:
            self.view.set_draw_mode('none')
        else:
            self.on_class_changed(self.list_classes.currentRow())
        self.refresh_boxes()

    def has_foreign_pending_draft(self, name):
        draft = self.universal_annotations.get(name, {})
        return bool(draft.get('sync_pending') and draft.get('draft_owner', self.collaborator_id) != self.collaborator_id)

    def update_reservation_status(self, record):
        self.lbl_lock_status.setToolTip('')
        state = record.get('status', 'available')
        if state == 'approved':
            text = 'Approved · annotations locked'
        elif state in ('locked', 'submitted') and expired(record):
            text = 'Reservation expired · read-only'
        elif tempo_hub._owned(record, self.collaborator_id):
            text = 'Awaiting review · editable' if state == 'submitted' else 'Draft · editable'
        else:
            text = 'Awaiting review · read-only' if state == 'submitted' else 'Not reserved by you · read-only'
        if state in ('locked', 'submitted'):
            try:
                end = datetime.datetime.fromisoformat(record['expires_at']).astimezone().strftime('%b %d, %Y at %H:%M')
                text += ' · Reservation ends ' + end
            except (KeyError, ValueError, TypeError):
                text += ' · Reservation expiry unavailable'
        if record.get('feedback'):
            text += '\nCorrections requested: ' + record['feedback']
        if self.current_file and self.has_foreign_pending_draft(os.path.basename(self.current_file)):
            draft = self.universal_annotations[os.path.basename(self.current_file)]
            owner = draft['draft_owner']
            name = tempo_hub.display_collaborator(owner, record=draft, profiles=self.lock_manifest.get('collaborators', {}))
            text = f'Read-only · unsynced annotations belong to {name}. Switch to that collaborator to sync them.'
            self.lbl_lock_status.setToolTip(owner)
        if getattr(self, '_annotation_session_conflict', False):
            text = 'Read-only · reload saved annotations'
        elif getattr(self, '_annotation_load_failed', False):
            text = 'Read-only · annotation loading failed'
        self.lbl_lock_status.setText(text)
        self.arrange_preview_header()
        
    def renew_current_lease(self):
        if not self.current_file:
            return
        self.reservation_error.clear()
        try:
            name = os.path.basename(self.current_file)
            if getattr(self,'_nuclear_only',False):
                if getattr(self,'_nuclear_busy',False):
                    return
                run_task(self,'Renewing nuclear reservation…',lambda:tempo_hub.renew_nuclear_annotations(
                    name,expected=getattr(self,'_nuclear_expected',None)))
                self.lock_manifest=tempo_hub.cached_lock_manifest()
                self._apply_current_lease()
                if self.universal_annotations[name].get('nuclear_sync_pending'):
                    self._sync_timer.start(1000)
                return
            run_task(self, 'Renewing reservation…', lambda: tempo_hub.renew_claim([name], self.collaborator_id))
            self.refresh_universal_file_list()
        except Exception as exc:
            self.reservation_error.set_error('The reservation could not be renewed. Review the details and current reservation status before trying again.', exc)

    def release_current_lease(self):
        if not self.current_file or not self.is_universal_mode:
            return
        self.reservation_error.clear()
        if (getattr(self, '_auto_pending', None) or self._sync_busy
                or getattr(self, '_annotation_save_unconfirmed', False)):
            self.reservation_error.set_error('Finish detection and saving before releasing this reservation.',
                                             RuntimeError('The current draft has pending work.'))
            return
        name = os.path.basename(self.current_file)
        record = self.lock_manifest.get('images', {}).get(name, {})
        if record.get('status') != 'locked' or record.get('claimed_by') != self.collaborator_id:
            return
        try:
            # Save and sync first so the next collaborator can adopt this draft.
            self.save_universal_annotations()
            draft = self.normalized_draft(name)
            sync_draft = tempo_hub._owned(record, self.collaborator_id) and not self.has_foreign_pending_draft(name)
            def release():
                if sync_draft:
                    tempo_hub.sync_annotation_drafts(self.collaborator_id, {name: draft})
                return tempo_hub.release_claim([name], self.collaborator_id)
            self._sync_busy = True
            self._sync_timer.stop()
            try:
                run_task(self, 'Releasing reservation…', release)
            finally:
                self._sync_busy = False
            self._reservation_revision = getattr(self, '_reservation_revision', 0) + 1
            self.lock_manifest['images'][name] = {
                key: value for key, value in record.items()
                if key not in ('claimed_by', 'claimed_at', 'expires_at', 'submission_id')}
            self.lock_manifest['images'][name]['status'] = 'available'
            if sync_draft:
                self.universal_annotations.get(name, {}).pop('sync_pending', None)
            self._apply_current_lease()
            self.refresh_universal_file_list(refresh=False)
            self.save_universal_annotations()
            self.submission_status.clear()
            if not any(draft.get('sync_pending') for draft in self.universal_annotations.values()):
                self.lbl_sync_status.clear()
            self.shared_browser.load()
        except Exception as exc:
            message = ('Release paused. Close other TEMPO windows, then choose Reload Saved Annotations beside the image list and retry Release Reservation.'
                       if isinstance(exc, AnnotationSessionConflict) else
                       'Release did not finish. Saved annotations are retained. Check the current reservation status before retrying.')
            self.reservation_error.set_error(message, exc)
            if self.universal_annotations.get(name, {}).get('sync_pending'):
                self._sync_timer.start(1000)

    def submission_selection(self):
        """One eligibility calculation powers the summary, button and submission."""
        payload, excluded = {}, {}
        for path in self.selected_annotation_paths():
            name = canonical_image_name(self.lock_manifest, os.path.basename(path))
            data = self.universal_annotations.get(name, {})
            rec = self.lock_manifest.get('images', {}).get(name, {})
            state = rec.get('status', 'available')
            reason = None
            if state == 'approved' or data.get('status') == 'approved':
                reason = 'already approved'
            elif state == 'submitted':
                reason = 'already awaiting review'
            elif state in ('locked', 'submitted') and expired(rec):
                reason = 'reservation expired'
            elif state == 'locked' and rec.get('claimed_by') != self.collaborator_id:
                reason = 'reserved by another account'
            elif not tempo_hub._owned(rec, self.collaborator_id):
                reason = 'not reserved by you'
            elif self.has_foreign_pending_draft(name):
                reason = 'unsynced annotations belong to another account'
            elif any(b.get('requires_viability_review') for b in data.get('boxes', [])):
                reason = 'unclassified cell outlines'
            elif not any(b.get('class_id') in (0, 1) for b in data.get('boxes', [])):
                reason = 'no cell annotations'
            elif data.get('image_id') not in (None, rec.get('image_id')):
                reason = 'image identity changed'
            else:
                image = QImage(os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, name))
                if image.isNull():
                    reason = 'image unavailable'
                else:
                    record = self.normalized_draft(name)
                    validate_annotations({name:record})
                    if name in payload:
                        reason = 'duplicate image selection'
                    else:
                        payload[name] = record
            if reason:
                excluded.setdefault(reason, []).append(data.get('original_filename') or name)
        return payload, excluded

    def submission_payload(self):
        return self.submission_selection()[0]

    def on_class_changed(self, index):
        if getattr(self,'_nuclear_only',False) and index != 2:
            self.list_classes.setCurrentRow(2)
            return
        if index < 0 or getattr(self, '_current_file_readonly', False): return
        if getattr(self, '_drawing_class', index) != index:
            self.view._cleanup_drawing()
        self._drawing_class = index
        color = self.class_config[index]['color']
        pen = QPen(color)
        pen.setWidth(2)
        self.view.set_draw_pen(pen)
        class_name = self.class_config[index]['name']
        self.view.set_draw_mode('polygon')
        
    def get_current_boxes(self):
        if self._preview_record is not None:
            return self._preview_record.get('boxes', [])
        if not self.current_file: return []
        if self.is_universal_mode:
            filename = os.path.basename(self.current_file)
            if filename in self.universal_annotations:
                return self.universal_annotations[filename].get("boxes", [])
            return []
        else:
            return self.annotations.get(self.current_file, [])

    def save_manual_annotation_edit(self, previous):
        self.clear_detection_feedback()
        self.file_error.clear()
        try:
            self.save_annotations_to_disk()
            self._annotation_save_unconfirmed = False
            self.btn_retry_annotation_save.hide()
        except OSError as exc:
            self._annotation_save_unconfirmed = True
            self._sync_timer.stop()
            self.btn_retry_annotation_save.show()
            self.file_error.set_error('Annotations could not be saved. Your edits remain on this image. Check storage permissions and available space, then retry saving before changing images.', exc)
        self.shared_browser.update_actions()

    def retry_manual_annotation_save(self):
        self.save_manual_annotation_edit(None)

    def add_polygon(self, pts):
        if not self.current_file or getattr(self, '_current_file_readonly', False):
            return
        previous = copy.deepcopy(self.get_current_boxes())
        class_id = self.list_classes.currentRow()
        if class_id < 0: class_id = 0 
        
        if getattr(self,'_nuclear_only',False) and class_id != 2:
            return
        new_box = {'class_id': class_id, 'polygon': pts}
        if class_id == 2:
            new_box['object_type'] = 'nucleus'
        
        if self.is_universal_mode:
            filename = os.path.basename(self.current_file)
            if filename not in self.universal_annotations:
                self.universal_annotations[filename] = {"boxes": [], "trained": False}
            self.universal_annotations[filename]["boxes"].append(new_box)
        else:
            if self.current_file not in self.annotations:
                self.annotations[self.current_file] = []
            self.annotations[self.current_file].append(new_box)
            
        self.save_manual_annotation_edit(previous)
        self.refresh_boxes()
        self.update_file_status()
        self.update_export_button_state()
        self.update_running_counts()
        self.update_submission_readiness()

    def update_submission_readiness(self):
        """Describe the selected images, independently of annotation counts."""
        if not self.is_universal_mode:
            return
        selected = self.selected_annotation_paths()
        self.btn_publish.setEnabled(False)
        self.lbl_readiness_badge.setToolTip('')
        if getattr(self, '_annotation_load_failed', False):
            self.lbl_readiness_badge.setText('Reload annotations before submitting images.')
            return
        if not selected:
            self.lbl_readiness_badge.setText('Select completed images to submit.')
            return
        try:
            payload, excluded = self.submission_selection()
            cells = validate_annotations(payload)[0] if payload else 0
            message = (f"{len(selected)} {'image' if len(selected) == 1 else 'images'} selected · "
                       f'{len(payload)} ready to submit')
            if payload:
                message += f' · {cells} cell annotations in ready images'
            if excluded:
                message += '\n' + '\n'.join(f'{len(names)} {reason}: '+', '.join(names[:3])
                             + (f' (+{len(names)-3} others)' if len(names) > 3 else '')
                             for reason,names in excluded.items())
                message += '\nSelect only images ready to submit.'
            self.lbl_readiness_badge.setText(message)
            self.lbl_readiness_badge.setToolTip('\n'.join(f'{reason}: '+', '.join(names)
                                                        for reason,names in excluded.items()))
            self.btn_publish.setEnabled(bool(payload) and not excluded)
        except ValueError as exc:
            self.lbl_readiness_badge.setText('Cannot submit this selection: ' + str(exc))

    def approve_selected_images(self, checked=False, names=None):
        self.submission_status.clear()
        self.approval_remote_succeeded = False
        if getattr(self, '_annotation_load_failed', False):
            self.submission_status.setText('Reload annotations before approving images.')
            return False
        if not self.is_universal_mode and names is None:
            return
        if self._sync_busy:
            self.submission_status.setText('Finishing annotation sync. Approve once it completes.')
            return
        try:
            records, paths = {}, {}
            selected_paths = ([os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, name) for name in names]
                              if names is not None else self.selected_annotation_paths())
            for path in selected_paths:
                name = os.path.basename(path)
                image = QImage(path)
                if image.isNull():
                    raise ValueError(f'Cannot read {name}.')
                records[name] = normalize_record(self.universal_annotations[name], image.width(), image.height())
                records[name].pop('sync_pending', None)
                records[name].pop('orphan_backup', None)
                paths[name] = path
            validate_annotations(records)
            details = '\n'.join(f"{record.get('original_filename') or name}: {len(record['boxes'])} annotations" for name, record in records.items())
            if not confirm_action(self, 'Approve Images',
                    details + '\n\nEvery cell must be annotated. Approval locks these annotations and makes the images eligible for training.',
                    'Approve Images'):
                return
            count = run_task(self, 'Approving selected images…', lambda: tempo_hub.approve_local_images(records, paths, self.collaborator_id))
            self.approval_remote_succeeded = True
            self._reservation_revision = getattr(self, '_reservation_revision', 0) + 1
            for name in records:
                self.lock_manifest.setdefault('images', {}).setdefault(name, {})['status'] = 'approved'
                self.universal_annotations[name]['status'] = 'approved'
                self.universal_annotations[name].pop('sync_pending', None)
            if self.current_file and os.path.basename(self.current_file) in records:
                self._current_file_readonly = True
                self.view.set_draw_mode('none')
                self.btn_delete.setEnabled(False)
                self.set_auto_buttons_enabled(False)
            self.save_universal_annotations()
            self.refresh_universal_file_list()
            self.submission_status.clear()
            return True
        except Exception as exc:
            if names is not None:
                raise
            message = ('Images were approved, but the local update failed. Annotations remain locked. Refresh image statuses before continuing.'
                       if self.approval_remote_succeeded else
                       'Approval did not finish. Refresh image statuses before retrying; remote approval may already have succeeded.')
            self.submission_error.set_error(message, exc)
            return False

    def publish_submission(self):
        if getattr(self, '_annotation_load_failed', False):
            self.update_submission_readiness()
            return
        if not self.is_universal_mode:
            return
        self.submission_status.clear()
        self.submission_error.clear()
        if self._sync_busy:
            self.submission_status.setText('Finishing annotation sync. Submit once it completes.')
            return
        try:
            self.lock_manifest = run_read_task(self, 'Checking reservations…', lambda: tempo_hub.fetch_lock_manifest(force_refresh=True))
            self.collaborator_id = tempo_hub.get_collaborator_id()
            payload, excluded = self.submission_selection()
            self.update_submission_readiness()
            if excluded:
                raise ValueError('\n'.join(f'{reason}: '+', '.join(names) for reason,names in excluded.items()))
            counts = validate_annotations(payload)
            if not confirm_action(self, 'Submit for Review',
                    f'Submit {counts[1]} images with {counts[0]} cell annotations? Every cell in each image must be annotated. '
                    'You can edit submitted images until approval; saved changes update the submission.', 'Submit Images'):
                return
            success, message = run_task(self, 'Submitting images…', lambda: tempo_hub.upload_submission(self.collaborator_id, list(payload), payload, {'counts':counts}))
            if not success:
                raise ValueError(message)
            self.submission_completed.emit()
            for name in payload:
                self.universal_annotations[name]['status'] = 'submitted'
            self.save_universal_annotations()
            self.refresh_universal_file_list()
            self.submission_status.clear()
            if not self.shared_browser.isHidden():
                self.shared_browser.load()
        except Exception as exc:
            self.submission_error.set_error('Submission did not finish. Review the details and image statuses before retrying Submit Selected.', exc)

    def open_checkout_dialog(self):
        if not self.can_change_annotation_image():
            return
        self.list_files.hide()
        self.images_heading.hide()
        self.shared_browser.show()
        self.shared_browser.load()

    def selected_annotation_paths(self):
        if self.is_universal_mode and not self.shared_browser.isHidden():
            return [os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, name)
                    for name in self.shared_browser.checked_names()]
        return [item.data(Qt.ItemDataRole.UserRole) for item in self.list_files.selectedItems()]

    def local_shared_previews(self):
        if not self.is_universal_mode:
            return {}
        shared = (self.shared_browser.catalog or {}).get('manifest',{}).get('images',{})
        return {name: dict(name=name, path=os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR,name),
                          record=copy.deepcopy(record), revision='local',
                          reservation=dict(self.lock_manifest.get('images',{}).get(name,{}),
                                           original_filename=record.get('original_filename') or name))
                for name, record in self.universal_annotations.items()
                if name not in shared and os.path.isfile(os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR,name))}

    def open_owned_shared_image(self, name):
        if not self.is_universal_mode or getattr(self, '_annotation_load_failed', False):
            return False
        catalog = self.shared_browser.catalog or {}
        reservation = catalog.get('manifest',{}).get('images',{}).get(name,{})
        current = self.lock_manifest.get('images',{}).get(name,{})
        path = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR,name)
        if (not tempo_hub._owned(reservation,self.collaborator_id)
                or not tempo_hub._owned(current,self.collaborator_id)
                or reservation.get('image_id') != current.get('image_id')
                or self.has_foreign_pending_draft(name) or name not in self.universal_annotations
                or not os.path.isfile(path)):
            return False
        for row in range(self.list_files.count()):
            item = self.list_files.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == path:
                self.list_files.setCurrentItem(item,QItemSelectionModel.SelectionFlag.NoUpdate)
                self.on_file_selected(item)
                return self.current_file == path and not self._current_file_readonly
        return False

    def loading_shared_preview(self, name):
        if (self.current_file and os.path.basename(self.current_file) == name
                and tempo_hub._owned(self.lock_manifest.get('images', {}).get(name, {}), self.collaborator_id)):
            self._apply_current_lease()
            return
        self.clear_detection_feedback()
        self.lbl_image_name.clear()
        self.lbl_image_name.hide()
        self.lbl_lock_status.clear()
        self.btn_renew.hide()
        self.btn_release.hide()
        self._current_file_readonly = True
        self.view.set_draw_mode('none')
        self.set_auto_buttons_enabled(False)
        self.btn_delete.setEnabled(False)
        self.canvas_hint.setText('Loading image preview…')
        self.center_stack.setCurrentWidget(self.canvas_hint)

    def show_shared_preview(self, payload):
        if tempo_hub.get_collaborator_id() != self.collaborator_id:
            self.refresh_account_identity()
            return
        if not self.can_change_annotation_image():
            return
        name = payload['name']
        if name != self._preview_name or self.current_file is not None:
            self.clear_detection_feedback()
            self.submission_status.clear()
        local = os.path.join(tempo_backend.UNIVERSAL_DATASET_DIR, name)
        record = payload['reservation']
        current_lease = self.lock_manifest.get('images', {}).get(name, {})
        # An owned, downloaded image always uses the regular local editing session,
        # including private edits that have not yet synced.
        if (tempo_hub._owned(record, self.collaborator_id)
                and tempo_hub._owned(current_lease, self.collaborator_id) and os.path.exists(local)
                and current_lease.get('image_id') == record.get('image_id')
                and name in self.universal_annotations):
            for row in range(self.list_files.count()):
                item = self.list_files.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == local:
                    self.list_files.setCurrentItem(item)
                    self.on_file_selected(item)
                    return
        self._preview_record = copy.deepcopy(payload['record'])
        self._preview_name = name
        self.current_file = None
        self._current_file_readonly = True
        self.lbl_image_name.setText(record.get('original_filename') or name)
        self.lbl_image_name.show()
        self.view.set_image(payload['path'])
        self.view.set_draw_mode('none')
        self.center_stack.setCurrentWidget(self.view)
        self.btn_renew.hide()
        self.btn_release.hide()
        self.reservation_actions.hide()
        self.set_auto_buttons_enabled(False)
        self.update_reservation_status(record)
        self.refresh_boxes()
        self.update_running_counts()

    def reserve_shared_images(self, names):
        if not names or not self.can_change_annotation_image():
            return
        browser = self.shared_browser
        if getattr(self, '_annotation_load_failed', False):
            browser.error.set_error('Reload annotations before reserving images.',
                                    OSError('Existing annotations could not be loaded.'))
            return
        browser.error.clear()
        browser.reservation_status.clear()
        try:
            downloaded, conflicts = run_task(self, 'Reserving and downloading images…',
                lambda: tempo_hub.checkout_batch(names, self.collaborator_id))
            self.lock_manifest = tempo_hub.cached_lock_manifest()
            if not self.load_universal_annotations():
                self._apply_current_lease()
                return
            self.refresh_universal_file_list(refresh=False)
            for name in downloaded:
                if name in browser.items:
                    browser.items[name].setCheckState(Qt.CheckState.Unchecked)
                for row in range(self.list_files.count()):
                    item = self.list_files.item(row)
                    if os.path.basename(item.data(Qt.ItemDataRole.UserRole)) == name:
                        self.list_files.setCurrentItem(item)
                        self.on_file_selected(item)
                        break
                if self.current_file:
                    browser.focused_name = name
                    break
            browser.load()
            if conflicts:
                browser.reservation_status.setText(f'{len(downloaded)} images downloaded; {len(conflicts)} unavailable. Refresh to review current reservations.')
            else:
                browser.reservation_status.setText(f'{len(downloaded)} images reserved and downloaded.')
        except Exception as exc:
            browser.reservation_status.setText('Reservation or download did not finish.')
            browser.error.set_error('Retry Reserve Image or Reserve Selected to resume downloads. Your current image and annotations are preserved.', exc)

    def update_overlay_visibility(self):
        self.view._mask_pick_cycle = None
        for item, box in zip(self.current_rect_items,self.get_current_boxes()):
            item.setVisible(self.show_nuclei.isChecked() if is_nucleus(box) else self.show_overlays.isChecked())

    def open_guidelines_dialog(self):
        GuidelinesDialog(self).exec()

    def auto_annotate_current_image(self):
        """Outline the current image with Cellpose."""
        self.start_auto_annotation("cellpose")

    def set_auto_buttons_enabled(self, enabled):
        nuclear_only = bool(getattr(self,'_nuclear_only',False))
        self.btn_publish.setVisible(not nuclear_only)
        self.lbl_readiness_badge.setVisible(not nuclear_only)
        self.submission_group.setTitle('Nuclear Reservation' if nuclear_only else 'Submit for Review')
        self.reservation_actions.setVisible(not self.btn_renew.isHidden() or not self.btn_release.isHidden())
        pending = bool(getattr(self, '_auto_pending', None))
        enabled = bool(enabled and self.current_file and not self._current_file_readonly and not pending)
        self.btn_restore_annotations.setEnabled(enabled and not getattr(self,'_nuclear_only',False))
        self.list_classes.setEnabled(bool(self.current_file and not self._current_file_readonly and not getattr(self,'_nuclear_only',False)))
        for button in (self.btn_mark_healthy, self.btn_mark_dead):
            button.setEnabled(bool(self.current_file and not self._current_file_readonly
                                   and not pending and self.list_boxes.selectedItems()))
        self.btn_auto_nano.setEnabled(bool(self.current_file) and not pending)
        approved = self.lock_manifest.get('images',{}).get(os.path.basename(self.current_file or ''),{}).get('status') == 'approved'
        self.btn_nuclear_edit.setVisible(bool(approved and self.is_universal_mode and not getattr(self,'_nuclear_only',False)))
        self.btn_nuclear_edit.setEnabled(bool(self.current_file and not pending))
        self.btn_auto_cellpose.setEnabled(enabled and not getattr(self,'_nuclear_only',False))
        self.btn_auto_stardist.setEnabled(enabled)
        self.btn_nuclear_submit.setEnabled(enabled and not getattr(self,'_nuclear_busy',False))
        self.shared_browser.update_actions()
        if enabled or pending or nuclear_only:
            self.auto_availability.clear()
        elif getattr(self, '_annotation_load_failed', False):
            self.auto_availability.setText('Reload annotations before detecting cells.')
        else:
            name = os.path.basename(self.current_file) if self.current_file else getattr(self, '_preview_name', None)
            reservation = self.lock_manifest.get('images', {}).get(name, {}) if self.is_universal_mode else {}
            preview = getattr(self, '_preview_record', None) or {}
            if reservation.get('status', preview.get('status')) == 'approved':
                reason = 'Approved annotations are locked.'
            elif name in self.shared_browser.local_entries:
                reason = 'This local image has no shared reservation.'
            elif self.is_universal_mode and name:
                if self.current_file and self.has_foreign_pending_draft(name):
                    reason = 'This image has another account’s unsynced annotations.'
                elif reservation.get('claimed_by') == self.collaborator_id and expired(reservation):
                    reason = 'Renew the reservation to generate annotations.'
                else:
                    reason = 'Reserve this image to generate annotations.'
            else:
                reason = 'Select an image to generate annotations.'
            self.auto_availability.setText(reason)

    def enforce_local_lease(self):
        if self.is_universal_mode:
            self.refresh_lease_status()
        self._apply_current_lease()

    def _apply_current_lease(self):
        if getattr(self,'_nuclear_only',False):
            lease = self.lock_manifest.get('nuclear_leases',{}).get(os.path.basename(self.current_file or ''),{})
            self._current_file_readonly = not tempo_hub._owned(lease,self.collaborator_id) or lease.get('status') != 'locked'
            self.btn_renew.setVisible(lease.get('claimed_by')==self.collaborator_id and lease.get('status')=='locked')
            self.btn_release.hide()
            status = ('Nuclear outlines awaiting review' if lease.get('status')=='submitted' else
                      'Nuclear reservation expired · renew to continue' if lease.get('claimed_by')==self.collaborator_id and self._current_file_readonly else
                      'Nuclear editing is unavailable · refresh the reservation' if self._current_file_readonly else
                      'Nuclear editing · approved cell outlines locked')
            self.lbl_lock_status.setText(status)
            if self._current_file_readonly:
                self.view.set_draw_mode('none')
            else:
                self.on_class_changed(2)
            self.set_auto_buttons_enabled(not self._current_file_readonly)
            return
        if self.is_universal_mode and self.current_file:
            rec = self.lock_manifest.get('images', {}).get(os.path.basename(self.current_file), {})
            self.btn_renew.setVisible(rec.get('status') in ('locked', 'submitted') and rec.get('claimed_by') == self.collaborator_id)
            self.btn_release.setVisible(rec.get('status') == 'locked' and rec.get('claimed_by') == self.collaborator_id)
            self.update_reservation_status(rec)
            if getattr(self, '_annotation_load_failed', False) or not tempo_hub._owned(rec, self.collaborator_id) or self.has_foreign_pending_draft(os.path.basename(self.current_file)):
                self._current_file_readonly = True
                self.view.set_draw_mode('none')
                self.btn_delete.setEnabled(False)
                self.set_auto_buttons_enabled(False)
            else:
                self._current_file_readonly = False
                self.btn_delete.setEnabled(bool(self.list_boxes.selectedItems()))
                self.set_auto_buttons_enabled(True)
                self.on_class_changed(self.list_classes.currentRow())
            self.update_submission_readiness()

    def start_auto_annotation(self, model_type="cellpose"):
        if (self._current_file_readonly and model_type != 'scout') or getattr(self, '_auto_pending', None):
            return
        if getattr(self,'_nuclear_only',False) and model_type == 'cellpose':
            return
        if hasattr(self, 'auto_worker') and not self.auto_worker.wait(0):
            return
        if not self.current_file:
            self.lbl_auto_status.setText('Select an image first.')
            return
        self.auto_error.clear()
        if model_type == 'cellpose' and self.get_current_boxes() and not confirm_action(self, 'Replace annotations?',
                'Cell outlines will be replaced. Nuclear outlines will be kept and require review against the new cells. A backup can be restored from this panel.',
                'Replace Annotations'):
            return
        self._auto_pending = {
            'file': self.current_file,
            'universal': self.is_universal_mode,
            'boxes': copy.deepcopy(self.get_current_boxes()),
        }
        self.lbl_auto_status.setText('Counting cells…' if model_type == 'scout' else 'Finding nuclei…' if model_type == 'stardist' else 'Outlining cells…')
        self.lbl_auto_status.hide()
        self._auto_pending['role'] = model_type
        self.auto_progress.begin(model_type)
        def reveal_progress(request=self._auto_pending):
            if request is getattr(self, '_auto_pending', None):
                scroll = self.annotation_inspector_scroll.verticalScrollBar()
                top = self.auto_progress.mapTo(self.annotation_inspector, QPoint(0, 0)).y()
                scroll.setValue(min(scroll.maximum(), max(0, top - 110)))
        QTimer.singleShot(0, reveal_progress)
        self.set_auto_buttons_enabled(False)
        self.auto_worker = AutoAnnotateWorker(self.current_file, model_type=model_type, parent=self)
        request = self._auto_pending
        self.auto_worker.status_signal.connect(lambda text: self.on_auto_annotation_status(text, request))
        self.auto_worker.stage_signal.connect(lambda stage: self.on_auto_annotation_stage(stage, request))
        self.auto_worker.finished_signal.connect(lambda result: self.on_auto_annotate_finished(result, request))
        self.auto_worker.error_signal.connect(lambda error: self.on_auto_annotate_error(error, request))
        self.auto_worker.start()

    def begin_nuclear_edit(self):
        if not self.current_file or not self.is_universal_mode or not self.can_change_annotation_image():
            return
        name = os.path.basename(self.current_file)
        try:
            record = run_task(self,'Reserving nuclear annotation…',lambda:tempo_hub.reserve_nuclear_annotations(name))
            self._nuclear_expected = record.pop('nuclear_expected',None)
            self.universal_annotations[name] = pixel_record(record)
            self.lock_manifest = tempo_hub.cached_lock_manifest()
            self._nuclear_only = True
            self._current_file_readonly = False
            self._preview_record = None
            self.list_classes.setCurrentRow(2)
            self.list_classes.setEnabled(False)
            self.on_class_changed(2)
            self.btn_nuclear_submit.show()
            self.refresh_boxes()
            self._apply_current_lease()
        except Exception as error:
            self.auto_error.set_error('Could not reserve nuclear annotations.',error)

    def sync_nuclear_layer(self):
        if getattr(self,'_nuclear_busy',False) or not self.current_file:
            return
        name = os.path.basename(self.current_file)
        record = self.universal_annotations[name]
        boxes_snapshot = copy.deepcopy(record['boxes'])
        if not record.get('nuclear_sync_pending'):
            return
        payload = self.normalized_draft(name)
        expected = copy.deepcopy(getattr(self,'_nuclear_expected',None))
        self._nuclear_busy = True
        def ready(value,error):
            self._nuclear_busy = False
            if error:
                self.sync_error.set_error('Nuclear edits remain local. Sync will retry in 30 seconds.',error)
                self._sync_timer.start(30000)
                return
            self._nuclear_expected = value
            if self.get_current_boxes() == boxes_snapshot:
                self.universal_annotations[name]['nuclear_sync_pending'] = False
                self.lbl_sync_status.clear()
            else:
                self.lbl_sync_status.setText('Nuclear edits pending sync')
                self._sync_timer.start(1000)
            self.save_universal_annotations()
            self.sync_error.clear()
        run_background(lambda:tempo_hub.save_nuclear_annotations(name,payload,expected=expected),ready)

    def submit_nuclear_layer(self):
        if (not getattr(self,'_nuclear_only',False) or getattr(self,'_nuclear_busy',False)
                or getattr(self,'_auto_pending',None) or self._current_file_readonly):
            return
        name = os.path.basename(self.current_file)
        try:
            payload = self.normalized_draft(name)
            run_task(self,'Submitting nuclear outlines…',lambda:tempo_hub.save_nuclear_annotations(
                name,payload,submit=True,expected=getattr(self,'_nuclear_expected',None)))
            self.universal_annotations[name]['nuclear_sync_pending'] = False
            self._current_file_readonly = True
            self.btn_nuclear_submit.hide()
            self.view.set_draw_mode('none')
            self.save_universal_annotations()
            self.set_auto_buttons_enabled(False)
        except Exception as error:
            self.submission_error.set_error('Nuclear submission could not finish. Your local edits are preserved.',error)

    def clear_scout_result(self):
        for item in getattr(self,'_scout_items',[]):
            if item.scene() is self.view.scene:
                self.view.scene.removeItem(item)
        self._scout_items = []

    def show_scout_result(self, result):
        self.clear_scout_result()
        from tempo_regions import region_counts
        counts = region_counts(result['boxes'])
        for box in result['boxes']:
            pen = QPen(QColor('#55C8FF')); pen.setWidth(2)
            if box.get('object_type') == 'group':
                x,y,w,h = box['box']
                item = self.view.scene.addRect(x,y,w,h,pen)
            else:
                x,y = box.get('center',np.mean(box['polygon'],axis=0))
                item = self.view.scene.addEllipse(x-3,y-3,6,6,pen)
            self._scout_items.append(item)
        groups = counts['healthy_groups'] + counts['dead_groups']
        text = f"Scout estimate: {counts['live_count']} healthy, {counts['dead_count']} dead"
        if groups:
            text += f" · {groups} groups; total cell count unknown"
        self.lbl_auto_status.setText(text)

    def clear_detection_feedback(self):
        self.auto_progress.finish()
        self.lbl_auto_status.clear()
        self.auto_error.clear()

    def auto_request_is_current(self, request):
        return bool(request and self.current_file == request['file']
                    and self.is_universal_mode == request['universal'])

    def on_auto_annotation_status(self, text, request):
        if (request is getattr(self, '_auto_pending', None) and self.auto_request_is_current(request)
                and self.get_current_boxes() == request['boxes']
                and (not self._current_file_readonly or request.get('role') == 'scout')):
            self.lbl_auto_status.setText(text)
            self.lbl_auto_status.hide()

    def on_auto_annotation_stage(self, stage, request):
        if (request is getattr(self, '_auto_pending', None) and self.auto_request_is_current(request)
                and self.get_current_boxes() == request['boxes']
                and (not self._current_file_readonly or request.get('role') == 'scout')):
            self.auto_progress.note_stage(stage)

    def on_auto_annotate_finished(self, json_response, request=None):
        pending = getattr(self, '_auto_pending', None)
        if request is not None and request is not pending:
            return
        request = pending
        self._auto_pending = None
        self.auto_progress.finish()
        self.lbl_auto_status.clear()
        self.set_auto_buttons_enabled(not self._current_file_readonly)
        if not self.auto_request_is_current(request):
            self.clear_detection_feedback()
            return
        if ((self._current_file_readonly and request.get('role') != 'scout') or self.current_file != request['file']
                or self.is_universal_mode != request['universal']
                or self.get_current_boxes() != request['boxes']):
            self.lbl_auto_status.setText('Result discarded; your current draft was preserved.')
            return
        filename = os.path.basename(self.current_file)
        if request.get('role') != 'scout' and not getattr(self,'_nuclear_only',False) and self.is_universal_mode and not tempo_hub._owned(self.lock_manifest.get('images', {}).get(filename, {}), self.collaborator_id):
            self.enforce_local_lease()
            self.lbl_auto_status.setText('Reservation is no longer active; draft preserved.')
            return
        try:
            if not json_response.get('success') or not isinstance(json_response.get('boxes'), list):
                raise ValueError(json_response.get('error', 'Invalid inference response.'))
            role = request.get('role','cellpose')
            if role == 'scout':
                self.show_scout_result(json_response)
                return
            boxes = copy.deepcopy(json_response['boxes'])
            if not boxes:
                self.lbl_auto_status.setText('No nuclei suggested; existing outlines kept.' if role == 'stardist' else 'No cells detected; existing outlines kept.')
                return
            image = QImage(self.current_file)
            normalized = normalize_record({'boxes': boxes}, image.width(), image.height())
            validate_annotations({filename: normalized}, allow_unclassified=True)
            boxes = pixel_record(normalized)['boxes']
            if role == 'stardist':
                from tempo_gui_assistance import merge_nuclear_proposals
                boxes = merge_nuclear_proposals(request['boxes'], boxes, image.width(),image.height())
            else:
                boxes = [box for box in boxes if not is_nucleus(box)] + [box for box in request['boxes'] if is_nucleus(box)]
                if self.is_universal_mode:
                    self.universal_annotations[filename]['nuclear_status'] = 'needs_review'
            # Keep a recoverable copy before replacing any saved draft.
            backup_dir = tempo_backend.UNIVERSAL_DATASET_DIR if self.is_universal_mode else self.current_dir
            backup = os.path.join(backup_dir, f'auto_annotation_backup_{time.time_ns()}.json')
            atomic_json(backup, {filename: {'boxes': request['boxes']}})
            old_record = copy.deepcopy(self.universal_annotations.get(filename)) if self.is_universal_mode else None
            if self.is_universal_mode:
                record = self.universal_annotations.setdefault(filename, {'trained': False})
                record['boxes'] = boxes
            else:
                self.annotations[self.current_file] = boxes
            try:
                self.save_annotations_to_disk()
            except Exception:
                if self.is_universal_mode:
                    if old_record is None:
                        self.universal_annotations.pop(filename, None)
                    else:
                        self.universal_annotations[filename] = old_record
                else:
                    self.annotations[self.current_file] = request['boxes']
                try:
                    self.save_annotations_to_disk()
                except Exception:
                    pass  # The pre-replacement backup remains available on disk.
                raise
            self.refresh_boxes()
            self.update_file_status()
            self.update_export_button_state()
            self.update_running_counts()
            self.update_submission_readiness()
            unclassified = sum(bool(box.get('requires_viability_review')) for box in boxes)
            if role == 'stardist':
                count=sum(is_nucleus(box) and box.get('proposal',False) for box in boxes)
                self.lbl_auto_status.setText(f'{count} nuclear proposals to review.')
            else:
                self.lbl_auto_status.setText(f'{len(boxes)} outlines generated · classify {unclassified} cells as healthy or dead.'
                                         if unclassified else f'{len(boxes)} annotations generated.')
        except Exception as exc:
            self.on_auto_annotate_error(str(exc))

    def on_auto_annotate_error(self, error_msg, request=None):
        pending = getattr(self, '_auto_pending', None)
        if request is not None and request is not pending:
            return
        self._auto_pending = None
        self.auto_progress.finish()
        self.set_auto_buttons_enabled(not self._current_file_readonly)
        if pending is not None and not self.auto_request_is_current(pending):
            self.clear_detection_feedback()
            return
        self.lbl_auto_status.setText('Auto-annotation failed; previous draft preserved.')
        self.auto_error.set_error('Cell detection did not finish. Review the details before retrying. Use Restore Backup if you need an earlier saved annotation state.', RuntimeError(error_msg))

    def restore_annotation_backup(self):
        self.restore_feedback.show()
        self.restore_error.clear()
        self.restore_status.clear()
        if not self.current_file or self._current_file_readonly or getattr(self, '_auto_pending', None):
            self.restore_status.setText('Select an editable image before restoring annotations.')
            return
        if self.is_universal_mode and not tempo_hub._owned(
                self.lock_manifest.get('images', {}).get(os.path.basename(self.current_file), {}), self.collaborator_id):
            self.restore_status.setText('An active reservation is required to restore annotations.')
            return
        folder = tempo_backend.UNIVERSAL_DATASET_DIR if self.is_universal_mode else self.current_dir
        path, _ = QFileDialog.getOpenFileName(self, 'Restore Annotation Backup', folder, 'Annotation backups (*.json)')
        if not path: return
        self.restore_error.clear()
        filename = os.path.basename(self.current_file)
        try:
            with open(path, encoding='utf-8') as stream:
                records = json.load(stream)
            if filename not in records:
                raise ValueError('This backup does not contain the selected image.')
            image = QImage(self.current_file)
            record = normalize_record(records[filename], image.width(), image.height())
            validate_annotations({filename:record}, allow_unclassified=True)
            if not confirm_action(self, 'Restore annotations?',
                    f'Replace this image’s annotations with {len(record["boxes"])} annotations from the backup?', 'Restore Annotations'):
                return
            old = copy.deepcopy(self.get_current_boxes())
            backup = os.path.join(folder, f'auto_annotation_backup_{time.time_ns()}.json')
            atomic_json(backup, {filename:{'boxes':old}})
            target = self.universal_annotations[filename] if self.is_universal_mode else self.annotations
            key = 'boxes' if self.is_universal_mode else self.current_file
            target[key] = pixel_record(record)['boxes']
            try:
                self.save_annotations_to_disk()
            except Exception:
                target[key] = old
                self.save_annotations_to_disk()
                raise
            self.clear_detection_feedback()
            self.refresh_boxes(); self.update_running_counts(); self.update_file_status(); self.update_submission_readiness()
            self.restore_status.setText('Annotations restored.')
        except Exception as exc:
            self.restore_status.setText('Annotation restoration did not finish.')
            self.restore_error.set_error('Review the details and select a valid backup for this image before retrying Restore Backup.', exc)

    def delete_box(self):
        if not self.current_file or getattr(self, '_current_file_readonly', False):
            return
        selected_items = self.list_boxes.selectedItems()
        if not selected_items: return
        
        previous = copy.deepcopy(self.get_current_boxes())
        indices = [item.data(Qt.ItemDataRole.UserRole) for item in selected_items]
        indices.sort(reverse=True) 
        
        if self.is_universal_mode:
            filename = os.path.basename(self.current_file)
            for idx in indices:
                if not getattr(self,'_nuclear_only',False) or is_nucleus(self.get_current_boxes()[idx]):
                    self.universal_annotations[filename]["boxes"].pop(idx)
        else:
            for idx in indices:
                self.annotations[self.current_file].pop(idx)
            
        self.save_manual_annotation_edit(previous)
        self.refresh_boxes()
        self.update_file_status()
        self.update_export_button_state()
        self.update_running_counts()
        self.update_submission_readiness()
            
    def refresh_boxes(self):
        self.list_boxes.clear()
        import PyQt6.QtWidgets as QtWidgets
        for item in self.view.scene.items():
            if item is self.view.temp_polygon_item or item is self.view.temp_box_item:
                continue
            if isinstance(item, (QGraphicsRectItem, QtWidgets.QGraphicsPolygonItem)):
                self.view.scene.removeItem(item)
                
        self.current_rect_items = []
        boxes = self.get_current_boxes()
        self.btn_delete.setEnabled(False)
        
        for i, data in enumerate(boxes):
            class_id = data['class_id']
            pts = data.get('polygon', [])
            if not pts and 'box' in data:
                # Fallback for old bb boxes to convert to polygons
                x, y, w, h = data['box']
                pts = [[x,y], [x+w,y], [x+w,y+h], [x,y+h]]
                data['polygon'] = pts
            
            class_name = 'unclassified' if data.get('requires_viability_review') else self.class_config[class_id]['name']
            
            item = QListWidgetItem(f"{class_name} - Shape {i+1} ({len(pts)} pts)")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.list_boxes.addItem(item)
            
            from PyQt6.QtGui import QPolygonF
            from PyQt6.QtCore import QPointF
            import PyQt6.QtWidgets as QtWidgets
            qpoly = QPolygonF([QPointF(p[0], p[1]) for p in pts])
            poly_item = QtWidgets.QGraphicsPolygonItem(qpoly)
            
            color = self.annotation_color(data)
            pen = QPen(color)
            pen.setWidth(2)
            poly_item.setPen(pen)
            
            brush_color = QColor(color)
            brush_color.setAlpha(80)
            poly_item.setBrush(brush_color)
            
            self.view.scene.addItem(poly_item)
            self.current_rect_items.append(poly_item)
        self.view.set_mask_items(self.current_rect_items)
        self.update_overlay_visibility()

    def select_mask_from_canvas(self, index, additive):
        item = self.list_boxes.item(index)
        if item is None:
            return
        selected = item.isSelected()
        flags = QItemSelectionModel.SelectionFlag.NoUpdate if additive else QItemSelectionModel.SelectionFlag.ClearAndSelect
        self.list_boxes.setCurrentItem(item, flags)
        if additive:
            item.setSelected(not selected)
        self.list_boxes.scrollToItem(item)
                
    def on_box_selection_changed(self):
        selected_indices = [item.data(Qt.ItemDataRole.UserRole) for item in self.list_boxes.selectedItems()]
        self.btn_delete.setEnabled(bool(selected_indices) and bool(self.current_file) and not self._current_file_readonly)
        for button in (self.btn_mark_healthy, self.btn_mark_dead):
            button.setEnabled(self.btn_delete.isEnabled() and not bool(getattr(self, '_auto_pending', None)))
        boxes = self.get_current_boxes()
        for i, rect_item in enumerate(self.current_rect_items):
            if i in selected_indices:
                pen = QPen(QColor(0, 255, 255)) 
                pen.setWidth(3)
                rect_item.setPen(pen)
            else:
                if i < len(boxes):
                    class_id = boxes[i]['class_id']
                    color = self.annotation_color(boxes[i])
                    pen = QPen(color)
                    pen.setWidth(2)
                    rect_item.setPen(pen)

    def annotation_color(self, box):
        return QColor(230, 170, 30, 180) if box.get('requires_viability_review') else self.class_config[box['class_id']]['color']

    def classify_selected_shapes(self, class_id):
        if not self.current_file or self._current_file_readonly or getattr(self, '_auto_pending', None):
            return
        indices = [item.data(Qt.ItemDataRole.UserRole) for item in self.list_boxes.selectedItems()]
        if not indices:
            return
        previous = copy.deepcopy(self.get_current_boxes())
        for index in indices:
            box = self.get_current_boxes()[index]
            if is_nucleus(box) or getattr(self,'_nuclear_only',False):
                continue
            box['class_id'] = class_id
            box.pop('requires_viability_review', None)
            box.pop('dead', None)
        self.save_manual_annotation_edit(previous)
        self.refresh_boxes()
        self.update_running_counts()
        self.update_file_status()
        self.update_export_button_state()
        self.update_submission_readiness()
                
    def update_file_status(self):
        if self.is_universal_mode:
            self.refresh_universal_file_list(refresh=False)
            return
        for i in range(self.list_files.count()):
            item = self.list_files.item(i)
            filepath = item.data(Qt.ItemDataRole.UserRole)
            base_name = os.path.basename(filepath)
            
            has_boxes = False
            if self.is_universal_mode:
                if base_name in self.universal_annotations and len(self.universal_annotations[base_name].get("boxes", [])) > 0:
                    has_boxes = True
            else:
                if filepath in self.annotations and len(self.annotations[filepath]) > 0:
                    has_boxes = True
                    
            if has_boxes:
                item.setText(base_name)
                item.setToolTip(f"Draft · {len(self.annotations.get(filepath, []))} annotations")
            else:
                item.setText(base_name)
                
    def update_export_button_state(self):
        selected = [item.data(Qt.ItemDataRole.UserRole) for item in self.list_files.selectedItems()]
        self.btn_export.setEnabled(not self.is_universal_mode and any(self.annotations.get(path) for path in selected))
        
    def update_running_counts(self):
        records = ([self.get_current_boxes()] if self._preview_record is not None else
                   [r.get('boxes', []) for r in self.universal_annotations.values()] if self.is_universal_mode else
                   self.annotations.values())
        pending = sum(bool(box.get('requires_viability_review')) for boxes in records for box in boxes)
        self.unclassified_count.setText(f'unclassified: {pending}')
        self.unclassified_count.setVisible(bool(pending))
        if self._preview_record is not None:
            self.counts_group.setTitle('Annotations in This Preview')
            for index, config in enumerate(self.class_config):
                count = sum(box['class_id'] == index and not box.get('requires_viability_review') for box in self.get_current_boxes())
                self.count_labels[config['name']].setText(f"{config['name']}: {count}")
            return
        self.counts_group.setTitle('Annotations in Loaded Images')
        counts = {c['name']: 0 for c in self.class_config}
        
        if self.is_universal_mode:
            for v in self.universal_annotations.values():
                for b in v.get("boxes", []):
                    if b.get('requires_viability_review'):
                        continue
                    c_id = b['class_id']
                    if c_id < len(self.class_config):
                        counts[self.class_config[c_id]['name']] += 1
        else:
            for boxes in self.annotations.values():
                for b in boxes:
                    if b.get('requires_viability_review'):
                        continue
                    c_id = b['class_id']
                    if c_id < len(self.class_config):
                        counts[self.class_config[c_id]['name']] += 1
                        
        for name, count in counts.items():
            if name in self.count_labels:
                self.count_labels[name].setText(f"{name}: {count}")
                
    def export_annotations(self):
        self.export_error.clear()
        self.export_status.clear()
        if self.is_universal_mode:
            self.lbl_sync_status.setText('Shared training data is prepared from approved images in Maintainer.')
            return False
        selected = [item.data(Qt.ItemDataRole.UserRole) for item in self.list_files.selectedItems()]
        files_to_export = [name for name in selected if self.annotations.get(name)]
        if not files_to_export:
            self.export_status.setText('Select annotated local images to export.')
            return False
        destination = QFileDialog.getExistingDirectory(self, 'Choose Export Folder')
        if not destination: return False
        if not confirm_action(self, 'Export local annotations?',
                f'Export {len(files_to_export)} selected images to a new dataset folder? This is a local export, independent of shared training approval.',
                'Export Images'):
            return False
        dataset_dir = None
        try:
            stems = [Path(path).stem.casefold() for path in files_to_export]
            if len(stems) != len(set(stems)):
                raise ValueError('Selected images share a filename stem. Rename them before exporting to avoid conflicting label files.')
            for path in files_to_export:
                image = QImage(path)
                if image.isNull():
                    raise ValueError(f'Cannot read {Path(path).name}.')
                record = normalize_record({'boxes': self.annotations[path]}, image.width(), image.height())
                validate_annotations({Path(path).name: record})
            dataset_dir = tempfile.mkdtemp(prefix='tempo-annotations-', dir=destination)
            exported = {}
            for filepath in files_to_export:
                filename = os.path.basename(filepath)
                shutil.copy(filepath, os.path.join(dataset_dir, filename))
                exported[filename] = copy.deepcopy(self.annotations[filepath])
            atomic_json(os.path.join(dataset_dir, 'annotations.json'),
                        {'format':'tempo-local-annotations-v3', 'images':exported})
            self.export_status.setText(f'Exported {len(exported)} images to {dataset_dir}')
            return True
        except Exception as exc:
            if dataset_dir is not None:
                shutil.rmtree(dataset_dir, ignore_errors=True)
            self.export_error.set_error('Export did not finish. Check the selected images and destination folder, then retry Export Selected Images.', exc)
            return False

def display_cell_name(identifier, kind="cell"):
    """Format identifiers for display without changing their identity."""
    text = str(identifier)
    if kind == 'group':
        number = re.fullmatch(r'(?:group|cell)?[ _-]*(\d+)', text, re.IGNORECASE)
        return 'Group ' + (number.group(1) if number else text)
    match = re.fullmatch(r'cell[ _-]*(\d+)', text, re.IGNORECASE)
    if match:
        return f'Cell {match.group(1)}'
    return f'Cell {text}' if text.isdigit() else text


class AnalyzerTab(QWidget):
    def __init__(self):
        super().__init__()
        self.df = None
        self.current_well_data = None
        self.current_well_std = None
        self.scatter_points = [] # Keep track of point data for clicking

        # Main Layout using a Splitter for adjustable panels
        layout = QVBoxLayout(self)
        self.analysis_toolbar = QWidget()
        self.analysis_toolbar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        toolbar = QGridLayout(self.analysis_toolbar)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setHorizontalSpacing(12)
        toolbar.setVerticalSpacing(6)
        layout.addWidget(self.analysis_toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        # ==========================================
        # LEFT PANEL: Plate Navigator
        # ==========================================
        self.plate_widget = QWidget()
        self.plate_layout = QVBoxLayout(self.plate_widget)
        self.plate_layout.addWidget(QLabel("96-Well Plate Navigator"))

        grid_widget = QWidget()
        self.grid_layout = QGridLayout(grid_widget)
        self.grid_layout.setSpacing(2)
        self.grid_layout.setContentsMargins(0, 0, 0, 0)
        self.well_buttons = {}
        rows = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H']
        
        # Build the 96-well grid of buttons
        for i, r in enumerate(rows):
            for c in range(1, 13):
                well_str = f"{r}{c:02d}"
                btn = QPushButton(f"{r}{c}")
                btn.setProperty("plateWell", True)
                btn.setFixedSize(32, 28)
                btn.setToolTip(f"Well {well_str}")
                btn.setEnabled(False) # Disabled until data is loaded
                btn.clicked.connect(lambda checked, well=well_str: self.load_well(well))
                self.grid_layout.addWidget(btn, i, c-1)
                self.well_buttons[well_str] = btn

        self.plate_layout.addWidget(grid_widget)
        self.plate_layout.addStretch()

        btn_load = QPushButton("Open Results CSV")
        set_button_color(btn_load, '#2D9CDB')
        btn_load.clicked.connect(self.load_csv)
        self.plate_layout.insertWidget(1, btn_load)

        self.plate_dialog = QDialog(self)
        self.plate_dialog.setWindowTitle('Select a Well')
        plate_dialog_layout = QVBoxLayout(self.plate_dialog)
        plate_dialog_layout.addWidget(self.plate_widget)

        # ==========================================
        # CENTER PANEL: Interactive Spatial Map
        # ==========================================
        self.map_widget = CompactWorkspace()
        map_layout = QVBoxLayout(self.map_widget)
        map_layout.setContentsMargins(0, 0, 0, 0)

        navigation_row = FlowRow()
        navigation = navigation_row.flow
        self.analysis_navigation = navigation
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.setSpacing(12)
        navigation.addWidget(btn_load)
        self.well_selector = QComboBox(); self.well_selector.setEnabled(False)
        self.well_selector.setMinimumContentsLength(5)
        self.well_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.well_selector.currentTextChanged.connect(lambda name: self.load_well(name) if name else None)
        self.field_selector = QComboBox()
        self.field_selector.setMinimumContentsLength(10)
        self.field_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.field_selector.currentIndexChanged.connect(self.load_field)
        self.field_selector.activated.connect(lambda _: self.load_field())
        for text, control in [('Well:', self.well_selector), ('View:', self.field_selector)]:
            group = QWidget(); row = QHBoxLayout(group); row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            label = QLabel(text)
            label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            row.addWidget(label); row.addWidget(control)
            navigation.addWidget(group)
        self.plate_toggle = QPushButton('Plate Navigator'); self.plate_toggle.setCheckable(True)
        self.plate_toggle.setToolTip('Choose a well on the plate')
        self.plate_toggle.setAccessibleName('Choose a well on the plate')
        self.plate_toggle.setEnabled(False)
        self.plate_toggle.toggled.connect(self.plate_dialog.setVisible)
        self.plate_dialog.finished.connect(lambda: self.plate_toggle.setChecked(False))
        navigation.addWidget(self.plate_toggle)
        self.quality_review_button = QPushButton('Recording Quality…')
        self.quality_review_button.setEnabled(False)
        self.quality_review_button.clicked.connect(self.open_quality_review)
        navigation.addWidget(self.quality_review_button)
        self.analysis_export_button=QPushButton('Export Analysis…')
        self.analysis_export_button.setEnabled(False)
        self.analysis_export_button.clicked.connect(self.export_analysis)
        navigation.addWidget(self.analysis_export_button)
        self.navigation_controls = (btn_load, self.well_selector, self.field_selector, self.plate_toggle)
        self.align_navigation_controls()
        toolbar.addWidget(navigation_row, 0, 0, 1, 4)

        # Heatmap Controls
        metric_label = QLabel('Map metric:')
        self.metric_combo = QComboBox()
        for field, label in [
            ('Peak_Amplitude_au', 'Amplitude (a.u.)'),
            ('Peak_Amplitude_AreaNorm_au', 'Area-normalized amplitude (a.u.)'),
            ('Peak_Amplitude_Fractional_dI_I0', 'Fractional intensity change (dI/I0)'),
            ('Time_to_Peak_ms', 'Time to peak (ms)'),
            ('Peak_to_Peak_Time_ms', 'Beat interval (ms)'),
            ('CT50_ms', 'Contraction time, 50% (ms)'),
            ('Max_Contraction_Velocity', 'Contraction intensity rate (a.u./ms)'),
            ('Fractional_Anisotropy', 'Fractional anisotropy'),
            ('Contraction_Angle_deg', 'Contraction angle (°)'),
            ('Vector_Coherence', 'Vector coherence'),
        ]:
            self.metric_combo.addItem(label, field)
        self.metric_combo.currentTextChanged.connect(self.update_map)
        self.metric_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.metric_combo.setMinimumContentsLength(12)

        # PyQtGraph Scatter Plot + Colorbar
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground('#1e1e1e')
        self.plot_item = add_plot(self.glw, title="Spatial FOV Map")
        tick_level = 0 if self.fontMetrics().height() > 22 else 2
        for side in ('left','bottom'):
            self.plot_item.getAxis(side).setStyle(maxTextLevel=tick_level)
            self.plot_item.getAxis(side).enableAutoSIPrefix(False)
        self.plot_item.setLabel('left', 'Y', units='px')
        self.plot_item.setLabel('bottom', 'X', units='px')
        
        # Display Y upward while preserving the recorded pixel coordinates.
        self.plot_item.getViewBox().invertY(False)
        
        self.scatter = pg.ScatterPlotItem(size=12, pen=pg.mkPen(None))
        self.scatter.sigClicked.connect(self.on_point_clicked)
        self.plot_item.addItem(self.scatter)

        # Add a ColorBarItem to the right of the plot
        self.color_bar = ColorBarItem(values=(0, 1), colorMap=pg.colormap.get('viridis'))
        self.color_bar.axis.setWidth(self.fontMetrics().horizontalAdvance('−1000.00') + 16)
        self.color_bar.axis.setStyle(maxTextLevel=tick_level)
        self.color_bar.sigLevelsChanged.connect(self.update_scatter_colors)
        self.glw.addItem(self.color_bar)
        self.color_bar.hide()
        self.analysis_status = QLabel('Open a results CSV to view cells and measurements.')
        self.analysis_status.setWordWrap(True)
        map_layout.addWidget(self.analysis_status)
        self.metadata_notice = InlineNotice()
        self.metadata_notice.setWordWrap(True)
        map_layout.addWidget(self.metadata_notice)
        self.spatial_notice = InlineNotice()
        map_layout.addWidget(self.spatial_notice)
        self.map_notice = InlineNotice()
        self.map_notice.setWordWrap(True)
        map_layout.addWidget(self.map_notice)
        self.analysis_error = ErrorDetails()
        map_layout.addWidget(self.analysis_error)
        self.well_cells = QTableWidget(0, 4)
        self.well_cells.setHorizontalHeaderLabels(['Cell', 'Field', 'Recording', 'Measurement'])
        self.well_cells.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.well_cells.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.well_cells.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.well_cells.horizontalHeader().setStretchLastSection(True)
        self.well_cells.cellClicked.connect(lambda row, _: self.show_cell(self.well_cells.item(row, 0).data(Qt.ItemDataRole.UserRole)))
        self.well_cells.hide()
        map_layout.addWidget(self.well_cells)
        map_layout.addWidget(self.glw)
        self.glw.setMinimumHeight(max(180, self.fontMetrics().height() * 10))
        self.glw.setStyleSheet("border: 1px solid #465467;")

        self.map_scroll = QScrollArea()
        self.map_scroll.setWidgetResizable(True)
        self.map_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.map_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.map_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.map_scroll.setWidget(self.map_widget)
        splitter.addWidget(self.map_scroll)

        # ==========================================
        # RIGHT PANEL: Cell Inspector Sidebar
        # ==========================================
        self.inspector_scroll = QScrollArea()
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_widget = QWidget()
        self.inspector_widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.inspector_layout = QVBoxLayout(self.inspector_widget)
        self.inspector_layout.setContentsMargins(8, 8, 8, 8)
        self.inspector_layout.setSpacing(6)
        self.inspector_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        self.lbl_target = QLabel("Select a cell on the map")
        self.lbl_target.setStyleSheet('font-weight: bold; color: #2D9CDB;')
        self.lbl_target.setWordWrap(True)
        inspector_heading = QHBoxLayout()
        inspector_heading.addWidget(self.lbl_target,1)
        self.inspector_layout.addLayout(inspector_heading)
        self.cell_context = QLabel(); self.cell_context.setWordWrap(True)
        self.inspector_layout.addWidget(self.cell_context)

        # Add Tabs for Means and Standard Deviations
        self.stats_tabs = StatisticsTabs()
        self.stats_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Maximum)
        self.tab_mean = QWidget()
        self.tab_std = QWidget()
        self.stats_tabs.addTab(self.tab_mean, "Means")
        self.stats_tabs.addTab(self.tab_std, "Standard Deviation")
        
        mean_layout = QVBoxLayout(self.tab_mean)
        std_layout = QVBoxLayout(self.tab_std)
        for stats_layout in (mean_layout, std_layout):
            stats_layout.setContentsMargins(4, 4, 4, 4)
            stats_layout.setSpacing(6)

        # Section A: Core Statistics (Mean)
        self.grp_core = QGroupBox("Core Statistics")
        self.form_core = MetricForm(self.grp_core)
        self.lbl_amp = QLabel("-")
        self.lbl_ttp = QLabel("-")
        self.lbl_rt = QLabel("-")
        self.lbl_p2p = QLabel("-")
        self.form_core.addRow("Amplitude (a.u.):", self.lbl_amp)
        self.form_core.addRow("Time-to-Peak (ms):", self.lbl_ttp)
        self.form_core.addRow("Relaxation (ms):", self.lbl_rt)
        self.form_core.addRow("Beat interval (ms):", self.lbl_p2p)
        mean_layout.addWidget(self.grp_core)

        # Section A: Core Statistics (Std Dev)
        self.grp_core_std = QGroupBox("Core Statistics")
        self.form_core_std = MetricForm(self.grp_core_std)
        self.lbl_amp_std = QLabel("-")
        self.lbl_ttp_std = QLabel("-")
        self.lbl_rt_std = QLabel("-")
        self.lbl_p2p_std = QLabel("-")
        self.form_core_std.addRow("Amplitude (a.u.):", self.lbl_amp_std)
        self.form_core_std.addRow("Time-to-Peak (ms):", self.lbl_ttp_std)
        self.form_core_std.addRow("Relaxation (ms):", self.lbl_rt_std)
        self.form_core_std.addRow("Beat interval (ms):", self.lbl_p2p_std)
        std_layout.addWidget(self.grp_core_std)

        # Section B: Fractional Kinetics (Mean)
        self.grp_kinetics = QGroupBox("Fractional Kinetics")
        self.form_kinetics = MetricForm(self.grp_kinetics)
        self.lbl_ct50 = QLabel("-")
        self.lbl_ct90 = QLabel("-")
        self.lbl_rt50 = QLabel("-")
        self.lbl_rt90 = QLabel("-")
        self.form_kinetics.addRow("CT50 (ms):", self.lbl_ct50)
        self.form_kinetics.addRow("CT90 (ms):", self.lbl_ct90)
        self.form_kinetics.addRow("RT50 (ms):", self.lbl_rt50)
        self.form_kinetics.addRow("RT90 (ms):", self.lbl_rt90)
        mean_layout.addWidget(self.grp_kinetics)

        # Section B: Fractional Kinetics (Std Dev)
        self.grp_kinetics_std = QGroupBox("Fractional Kinetics")
        self.form_kinetics_std = MetricForm(self.grp_kinetics_std)
        self.lbl_ct50_std = QLabel("-")
        self.lbl_ct90_std = QLabel("-")
        self.lbl_rt50_std = QLabel("-")
        self.lbl_rt90_std = QLabel("-")
        self.form_kinetics_std.addRow("CT50 (ms):", self.lbl_ct50_std)
        self.form_kinetics_std.addRow("CT90 (ms):", self.lbl_ct90_std)
        self.form_kinetics_std.addRow("RT50 (ms):", self.lbl_rt50_std)
        self.form_kinetics_std.addRow("RT90 (ms):", self.lbl_rt90_std)
        std_layout.addWidget(self.grp_kinetics_std)

        # Section C: Kinematic Velocities (Mean)
        self.grp_vel = QGroupBox("Intensity Change Rate")
        self.form_vel = MetricForm(self.grp_vel)
        self.lbl_cvel = QLabel("-")
        self.lbl_rvel = QLabel("-")
        self.form_vel.addRow("Contraction (a.u./ms):", self.lbl_cvel)
        self.form_vel.addRow("Relaxation (a.u./ms):", self.lbl_rvel)
        mean_layout.addWidget(self.grp_vel)

        # Section C: Kinematic Velocities (Std Dev)
        self.grp_vel_std = QGroupBox("Intensity Change Rate")
        self.form_vel_std = MetricForm(self.grp_vel_std)
        self.lbl_cvel_std = QLabel("-")
        self.lbl_rvel_std = QLabel("-")
        self.form_vel_std.addRow("Contraction (a.u./ms):", self.lbl_cvel_std)
        self.form_vel_std.addRow("Relaxation (a.u./ms):", self.lbl_rvel_std)
        std_layout.addWidget(self.grp_vel_std)

        # Section D: Contractile Anisotropy & Direction (Mean)
        self.grp_anisotropy = QGroupBox("Contraction Direction")
        self.form_anisotropy = MetricForm(self.grp_anisotropy)
        self.lbl_fa = QLabel("-")
        self.lbl_angle = QLabel("-")
        self.lbl_coherence = QLabel("-")
        self.form_anisotropy.addRow("Fractional Anisotropy (α):", self.lbl_fa)
        self.form_anisotropy.addRow("Contraction Angle (θ):", self.lbl_angle)
        self.form_anisotropy.addRow("Vector Coherence:", self.lbl_coherence)
        mean_layout.addWidget(self.grp_anisotropy)

        # Section D: Contractile Anisotropy (Std Dev)
        self.grp_anisotropy_std = QGroupBox("Direction Variability")
        self.form_anisotropy_std = MetricForm(self.grp_anisotropy_std)
        self.lbl_fa_std = QLabel("-")
        self.lbl_angle_std = QLabel("-")
        self.lbl_coherence_std = QLabel("-")
        self.form_anisotropy_std.addRow("Fractional Anisotropy (α):", self.lbl_fa_std)
        self.form_anisotropy_std.addRow("Contraction Angle (θ):", self.lbl_angle_std)
        self.form_anisotropy_std.addRow("Vector Coherence:", self.lbl_coherence_std)
        std_layout.addWidget(self.grp_anisotropy_std)

        self.metric_forms = (self.form_core, self.form_core_std, self.form_kinetics,
                             self.form_kinetics_std, self.form_vel, self.form_vel_std,
                             self.form_anisotropy, self.form_anisotropy_std)
        from tempo_ui import ContextHelpPanel
        self.statistics_help = ContextHelpPanel()
        for form in self.metric_forms:
            form.setVerticalSpacing(6)
            form.setContentsMargins(8, 6, 8, 6)
            form.parentWidget().setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            for row in range(form.rowCount()):
                label = form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
                label.setWordWrap(True)
                label.setMinimumWidth(150)
                label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                dispersion = form in (self.form_core_std, self.form_kinetics_std, self.form_vel_std, self.form_anisotropy_std)
                explanation = statistic_description(label.text())
                if dispersion:
                    explanation += ' Standard deviation describes variation between beats for a selected cell, or between cell averages for the full well.'
                install_help(label, explanation, panel=self.statistics_help,
                             title=('Standard deviation — ' if dispersion else '') + label.text())
                value = form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
                value.setMinimumWidth(0)
                value.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        mean_layout.addStretch(); std_layout.addStretch()
        self.stats_tabs.set_forms(((self.form_core, self.form_kinetics, self.form_vel, self.form_anisotropy),
                                   (self.form_core_std, self.form_kinetics_std, self.form_vel_std, self.form_anisotropy_std)))
        self.inspector_layout.addWidget(self.stats_tabs)
        self.inspector_layout.addWidget(self.statistics_help, 1)

        self.grp_wave = QGroupBox('Waveforms')
        wave_layout = QVBoxLayout(self.grp_wave)
        self.grp_wave.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.wave_tabs = ContentTabs()
        summary_page = QWidget(); summary_layout = QVBoxLayout(summary_page)
        summary_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary_note = QLabel()
        self.summary_note.hide()
        self.summary_note.setWordWrap(True)
        self.summary_note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        summary_layout.addWidget(self.summary_note)
        self.summary_plot = PlotWidget(background='#1e1e1e')
        signal_plot_height = max(160, self.fontMetrics().height() * 7 + 8)
        self.signal_plot_height = signal_plot_height
        self.summary_plot.setMinimumWidth(0); self.summary_plot.setFixedHeight(signal_plot_height)
        self.summary_plot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.summary_plot.setLabel('bottom', 'Time (ms)'); self.summary_plot.setLabel('left', 'Amplitude<br>(a.u.)')
        self.summary_plot.fit_label_height(signal_plot_height)
        for side in ('left','bottom'):
            self.summary_plot.getPlotItem().getAxis(side).setStyle(maxTextLevel=tick_level)
        self.summary_curve = self.summary_plot.plot(pen=pg.mkPen('#79C0FF', width=2))
        summary_layout.addWidget(self.summary_plot)
        self.wave_tabs.addTab(summary_page, 'Illustrative Beat')
        self.summary_info = InformationButton('Illustrative beat',
            'Generated from summary measurements; not a recorded signal.')
        self.wave_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, InformationSlot(self.summary_info))
        recorded_page = QWidget(); recorded_layout = QVBoxLayout(recorded_page)
        recorded_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.wave_note = QLabel('Select a cell to view its recorded signal.')
        self.wave_note.setWordWrap(True)
        self.wave_note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        recorded_layout.addWidget(self.wave_note)
        self.trace_signal = QComboBox()
        self.trace_signal.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.trace_signal.addItems(['Contraction (Raw a.u.)', 'Contraction (Area-Norm a.u.)', 'Contraction (Fractional dI/I0)', 'Speed (a.u.)'])
        self.trace_signal.currentTextChanged.connect(lambda: self.draw_recorded_wave(getattr(self, '_trace_row', None)))
        recorded_layout.addWidget(self.trace_signal)
        self.cell_selector = QComboBox()
        self.cell_selector.setPlaceholderText('Choose a cell…')
        self.cell_selector.setMinimumContentsLength(18)
        self.cell_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cell_selector.currentIndexChanged.connect(self.select_analysis_cell)
        self.measurement_label = QLabel('Cell:')
        self.map_controls = SelectorRow(metric_label,self.metric_combo,self.measurement_label,self.cell_selector)
        toolbar.addWidget(self.map_controls,1,0,1,4)
        self.measurement_controls = FlowRow()
        self.measurement_controls.flow.addWidget(QLabel('Measurements:'))
        self.measurement_selector = QComboBox()
        self.measurement_selector.addItem('Cells', 'cell')
        self.measurement_selector.addItem('Groups', 'group')
        self.measurement_controls.flow.addWidget(self.measurement_selector)
        self.measurement_selector.currentIndexChanged.connect(lambda: self.load_well(self.well_selector.currentText()) if self.df is not None else None)
        self.measurement_controls.hide()
        toolbar.addWidget(self.measurement_controls,2,0,1,4)
        self.mini_plot = PlotWidget(background='#1e1e1e')
        self.mini_plot.setFixedHeight(signal_plot_height); self.mini_plot.setMinimumWidth(0)
        self.mini_plot.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        for side in ('left','bottom'):
            self.mini_plot.getPlotItem().getAxis(side).setStyle(maxTextLevel=tick_level)
        self.wave_curve = self.mini_plot.plot(pen=pg.mkPen('#7CE4BA', width=2))
        recorded_layout.addWidget(self.mini_plot)
        self.wave_tabs.addTab(recorded_page, 'Recorded Signal')
        wave_layout.addWidget(self.wave_tabs)
        # The signal shares the map column's width; statistics retain a compact inspector.
        map_layout.addWidget(self.grp_wave)
        self.mini_plot.hide(); self.trace_signal.setEnabled(False)
        self.summary_plot.hide()

        self.inspector_scroll.setWidget(self.inspector_widget)

        splitter.addWidget(self.inspector_scroll)

        # Set panel width ratios (20% Grid, 50% Map, 30% Inspector)
        self.plate_widget.setMinimumWidth(426)
        self.plate_widget.setMaximumWidth(426)
        self.inspector_scroll.setMinimumWidth(320)
        self.inspector_scroll.setMaximumWidth(max(420, self.fontMetrics().horizontalAdvance('Fractional Anisotropy (α):') + self.fontMetrics().horizontalAdvance('100000.0000') + 80))
        self.inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        splitter.setSizes([680, 420])
        self.apply_display_preferences()

    # ==========================================
    # LOGIC AND INTERACTIVITY
    # ==========================================
    def align_navigation_controls(self):
        controls = getattr(self, 'navigation_controls', ())
        if controls:
            height = max(control.sizeHint().height() for control in controls)
            for control in controls:
                control.setFixedHeight(height)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.align_navigation_controls()

    def apply_display_preferences(self):
        metric = tempo_preferences.get('analyzer', 'default_metric', 'Peak_Amplitude_au')
        index = self.metric_combo.findData(metric)
        palette = tempo_preferences.get('analyzer', 'colormap_palette', 'viridis')
        if palette not in ('viridis', 'plasma', 'inferno', 'magma'):
            palette = 'viridis'
        self.map_colormap = pg.colormap.get(palette)
        self.color_bar.setColorMap(self.map_colormap)
        if index >= 0:
            self.metric_combo.setCurrentIndex(index)
        if self.current_well_data is not None:
            self.update_map()

    def load_csv(self, file_path=None):
        if not isinstance(file_path, str):
            file_path, _ = QFileDialog.getOpenFileName(
                self,
                "Open Results CSV",
                tempo_backend.PROCESSED_DIR,
                "CSV Files (*.csv)",
            )
        if file_path:
            self.analysis_error.clear()
            try:
                candidate = read_results(file_path)
                self.df = candidate
            except Exception as e:
                self.analysis_error.set_error("Could not open this results CSV. Choose a TEMPO results file with well and cell identifiers. Any previously loaded results are unchanged.", e)
                return
                
            self.dataset_path = file_path
            self.quality_review_button.setEnabled(True)
            self.analysis_export_button.setEnabled(True)
            self.spatial_registrations = {}
            self.spatial_warnings = []
            self.analysis_status.setText(os.path.basename(file_path))
            missing_identity = candidate[['Recording_ID', 'FOV_ID']].eq('Unknown').any().any()
            unlocated = candidate[['Centroid_X', 'Centroid_Y']].isna().any(axis=1).sum()
            metadata = []
            if missing_identity:
                metadata.append('Recording or field identity is unavailable for some cells.')
            if unlocated:
                metadata.append(f'{unlocated} cells have no spatial coordinates; their measurements remain selectable.')
            if 'Quality_Flags' in candidate:
                flagged=candidate.loc[candidate['Quality_Flags'].fillna('').astype(str).ne(''),'Recording_ID'].nunique()
                if flagged:
                    metadata.append(f'{flagged} recordings have quality flags. Review inclusion in Recording Quality.')
            if 'Quality_Included' in candidate:
                excluded=candidate.loc[candidate['Quality_Included'].astype(str).str.lower().isin(('false','0')),'Recording_ID'].nunique()
                if excluded:metadata.append(f'{excluded} recordings are excluded from summaries.')
            if 'Source_Pixel_Dtype' in candidate and candidate['Source_Pixel_Dtype'].dropna().nunique()>1:
                metadata.append('Raw intensity scales differ between recordings. Use fractional measurements for comparisons.')
            self.metadata_notice.setText(' '.join(metadata))
            self.current_well_data = self.current_well_std = self.current_field_data = None
            for selector in (self.field_selector, self.cell_selector):
                selector.blockSignals(True)
                selector.clear()
                selector.blockSignals(False)
            for label in (self.lbl_amp, self.lbl_ttp, self.lbl_rt, self.lbl_p2p,
                          self.lbl_ct50, self.lbl_ct90, self.lbl_rt50, self.lbl_rt90,
                          self.lbl_cvel, self.lbl_rvel, self.lbl_fa, self.lbl_angle, self.lbl_coherence,
                          self.lbl_amp_std, self.lbl_ttp_std, self.lbl_rt_std, self.lbl_p2p_std,
                          self.lbl_ct50_std, self.lbl_ct90_std, self.lbl_rt50_std, self.lbl_rt90_std,
                          self.lbl_cvel_std, self.lbl_rvel_std, self.lbl_fa_std,
                          self.lbl_angle_std, self.lbl_coherence_std):
                label.setText('-')
            self.lbl_target.setText('Select a cell on the map')
            self.summary_curve.clear(); self.summary_plot.hide()
            self.summary_note.clear(); self.summary_note.hide()
            self.draw_recorded_wave(None)
            self.update_map()
            self.glw.show()

            # Activate buttons for wells that actually have data
            active_wells = self.df['Well_ID'].dropna().unique()
            self.well_selector.blockSignals(True)
            self.well_selector.clear(); self.well_selector.addItems([str(name) for name in active_wells])
            self.well_selector.blockSignals(False)
            self.well_selector.setEnabled(bool(len(active_wells)))
            self.plate_toggle.setEnabled(bool(len(active_wells)))
            for well_id, btn in self.well_buttons.items():
                if well_id in active_wells:
                    btn.setEnabled(True)
                    set_button_color(btn, ACTION_GREEN)
                else:
                    btn.setEnabled(False)
                    set_button_color(btn, '')
            
            # Auto-load the first available well
            if len(active_wells) > 0:
                self.load_well(active_wells[0])
            else:
                self.map_notice.setText('This results file contains no cell measurements.')

    def open_quality_review(self):
        if self.df is None or not getattr(self,'dataset_path',None):
            return
        from tempo_quality_ui import QualityReviewDialog
        well=self.well_selector.currentText()
        field=self.field_selector.currentData()
        dialog=QualityReviewDialog(self.dataset_path,self.df,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.load_csv(self.dataset_path)
            if self.well_selector.findText(well)>=0:self.load_well(well)
            index=self.field_selector.findData(field)
            if index>=0:self.field_selector.setCurrentIndex(index)

    def export_analysis(self):
        if not getattr(self,'dataset_path',None):return
        destination,accepted=QFileDialog.getSaveFileName(self,'Export reviewed analysis',
            str(Path(self.dataset_path).with_suffix('.zip')),'Analysis archive (*.zip)')
        if not destination:return
        from tempo_analysis_io import export_bundle
        try:run_task(self,'Exporting analysis…',lambda:export_bundle(self.dataset_path,destination))
        except Exception as exc:self.analysis_error.set_error('Analysis export could not finish. Existing results were preserved.',exc)

    def load_well(self, well_id):
        if self.df is None: return
        self.well_selector.blockSignals(True); self.well_selector.setCurrentText(str(well_id)); self.well_selector.blockSignals(False)
        self.plot_item.setTitle(f"Spatial FOV Map - Well {well_id}")
        
        # Filter dataframe for this well
        well_df = self.df[self.df['Well_ID'] == well_id].copy()
        kinds = set(well_df['Object_Type'])
        self.measurement_selector.blockSignals(True)
        for position, kind in enumerate(('cell', 'group')):
            self.measurement_selector.model().item(position).setEnabled(kind in kinds)
        if self.measurement_selector.currentData() not in kinds and kinds:
            self.measurement_selector.setCurrentIndex(0 if 'cell' in kinds else 1)
        self.measurement_selector.blockSignals(False)
        self.measurement_controls.setVisible(len(kinds) > 1)
        kind = self.measurement_selector.currentData()
        self.measurement_label.setText('Group:' if kind == 'group' else 'Cell:')
        well_df = well_df[well_df['Object_Type'] == kind].copy()
        
        # Ignore the 0.0 base value for the first beat if loading raw uncompiled beats (e.g. Assay_Master_Data.csv)
        if 'Peak_to_Peak_Time_ms' in well_df.columns:
            well_df['Peak_to_Peak_Time_ms'] = well_df['Peak_to_Peak_Time_ms'].replace(0.0, np.nan)
            
        self.current_well_data, self.current_well_std = summarize_cells(well_df)
        self.field_selector.blockSignals(True)
        self.field_selector.clear()
        self.field_selector.addItem('Full Well', None)
        self.spatial_registrations = {}
        self.spatial_warnings = []
        for recording, field in self.current_well_data[['Recording_ID', 'FOV_ID']].drop_duplicates().itertuples(index=False, name=None):
            label = f'Field {field}' if field != 'Unknown' else 'Field unknown'
            if self.current_well_data[self.current_well_data.FOV_ID == field].Recording_ID.nunique() > 1:
                label += f' · {recording}'
            self.field_selector.addItem(label, (recording, field))
            self.field_selector.setItemData(self.field_selector.count()-1, f'Recording: {recording}', Qt.ItemDataRole.ToolTipRole)
            rows = self.current_well_data[(self.current_well_data.Recording_ID == recording) & (self.current_well_data.FOV_ID == field)]
            try:
                associations = rows.get('Spatial_Metadata_File', pd.Series(dtype=str))
                associated = associations.map(lambda value: isinstance(value, str) and bool(value.strip()))
                paths = associations[associated].unique()
                if len(paths) > 1:
                    raise ValueError('Conflicting spatial associations for one field.')
                document = registration_for_row(self.dataset_path, rows[associated].iloc[0]) if len(paths) else None
                if document:
                    self.spatial_registrations[(recording, field)] = document
            except (OSError, ValueError, KeyError) as exc:
                self.spatial_warnings.append(f'{label}: {exc}')
        self.field_selector.blockSignals(False)
        self.build_field_map()
        self.spatial_notice.setText('Some field positions could not be loaded. Select a field to view its image map.' if self.spatial_warnings else '')
        self.spatial_notice.setToolTip('\n'.join(self.spatial_warnings))
        self.load_field()

    def return_to_well(self):
        if self.current_well_data is not None:
            self.field_selector.blockSignals(True); self.field_selector.setCurrentIndex(0)
            self.field_selector.blockSignals(False)
            self.load_field()

    def build_field_map(self):
        self.field_curves = {}
        systems = {document['coordinate_system'] for document in self.spatial_registrations.values()}
        geometries = {json.dumps(document.get('well_geometry'), sort_keys=True) for document in self.spatial_registrations.values() if document.get('well_geometry')}
        if len(systems) > 1 or len(geometries) > 1:
            self.spatial_warnings.append('Field registrations use incompatible well coordinate systems or geometry; placement is unavailable.')
            self.spatial_registrations = {}
    def select_field_identity(self, identity):
        for index in range(self.field_selector.count()):
            if self.field_selector.itemData(index) == identity:
                self.field_selector.setCurrentIndex(index)
                return

    def fill_well_table(self, data, metric):
        self.well_cells.setRowCount(len(data))
        self.well_cells.setHorizontalHeaderLabels(['Group' if self.measurement_selector.currentData() == 'group' else 'Cell', 'Field', 'Recording', self.metric_combo.currentText()])
        for table_row, (index, row) in enumerate(data.iterrows()):
            value = row.get(metric, np.nan)
            texts = [display_cell_name(row['Cell_ID'], row.get('Object_Type', 'cell')), str(row['FOV_ID']), str(row['Recording_ID']),
                     f'{value:.4g}' if pd.notna(value) else 'Unavailable']
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, int(index))
                item.setToolTip(text)
                self.well_cells.setItem(table_row, column, item)

    def load_field(self, *_):
        if self.current_well_data is None:
            return
        identity = self.field_selector.currentData()
        if identity is None:
            self.current_field_data = self.current_well_data
            field = 'Full Well'
        else:
            recording, field = identity
            self.current_field_data = self.current_well_data[
                (self.current_well_data['Recording_ID'] == recording) & (self.current_well_data['FOV_ID'] == field)]
        self.cell_selector.blockSignals(True)
        self.cell_selector.clear()
        self.cell_selector.addItem('Full Well', None)
        noun = 'groups' if self.measurement_selector.currentData() == 'group' else 'cells'
        self.cell_selector.setItemData(0, f'Show statistics across all {noun} in this well. The selected map view is retained.', Qt.ItemDataRole.ToolTipRole)
        for index, row in self.current_field_data.iterrows():
            name = display_cell_name(row['Cell_ID'], row.get('Object_Type', 'cell'))
            self.cell_selector.addItem(name if identity is not None else f"{name} · Field {row['FOV_ID']} · {row['Recording_ID']}", int(index))
            self.cell_selector.setItemData(self.cell_selector.count()-1, f"{name} · Field {row['FOV_ID']} · Recording {row['Recording_ID']}", Qt.ItemDataRole.ToolTipRole)
        self.cell_selector.setCurrentIndex(0)
        self.cell_selector.blockSignals(False)
        self.plot_item.setTitle('Full Well' if identity is None else f'Field {field}')
        self.draw_recorded_wave(None)
        # Preferences may refer to a metric absent from historical CSVs or this field.
        metric = self.metric_combo.currentData()
        if metric not in self.current_field_data or not self.current_field_data[metric].notna().any():
            available = next((i for i in range(self.metric_combo.count())
                              if self.metric_combo.itemData(i) in self.current_field_data
                              and self.current_field_data[self.metric_combo.itemData(i)].notna().any()), None)
            if available is not None:
                self.metric_combo.blockSignals(True)
                self.metric_combo.setCurrentIndex(available)
                self.metric_combo.blockSignals(False)
        self.update_map()
        self.show_well_averages(self.well_selector.currentText())

    def select_analysis_cell(self, *_):
        index = self.cell_selector.currentData()
        if index is not None:
            self.show_cell(int(index))
        elif self.cell_selector.currentIndex() >= 0:
            self.show_well_averages(self.well_selector.currentText())

    def show_well_averages(self, well_id):
        if self.current_well_data is None or self.current_well_data.empty: return
        self.well_cells.clearSelection()
        self.stats_tabs.show()
        
        # Calculate the mean across all cells in this well
        well_avg = self.current_well_data.mean(numeric_only=True)
        well_std = self.current_well_data.std(numeric_only=True)
        if 'Contraction_Angle_deg' in self.current_well_data:
            well_avg['Contraction_Angle_deg'], well_std['Contraction_Angle_deg'] = axial_statistics(self.current_well_data['Contraction_Angle_deg'])
        num_cells = len(self.current_well_data)
        
        # Update Header to indicate it's a Well Average
        noun = "group" if self.measurement_selector.currentData() == "group" else "cell"
        count_label = noun if num_cells == 1 else noun + "s"
        self.lbl_target.setText(f"Well Averages: {well_id} ({num_cells} {count_label})")
        self.cell_context.setText('Select a group on the map or from the group list.' if noun == 'group' else 'Select a cell on the map or from the cell list.')
        
        def fmt(val, decimals=2):
            return f"{val:.{decimals}f}" if pd.notnull(val) else "N/A"

        self.lbl_amp.setText(fmt(well_avg.get('Peak_Amplitude_au'), 2))
        self.lbl_ttp.setText(fmt(well_avg.get('Time_to_Peak_ms'), 1))
        self.lbl_rt.setText(fmt(well_avg.get('Relaxation_Time_ms'), 1))
        self.lbl_p2p.setText(fmt(well_avg.get('Peak_to_Peak_Time_ms'), 1))
        
        self.lbl_ct50.setText(fmt(well_avg.get('CT50_ms'), 1))
        self.lbl_ct90.setText(fmt(well_avg.get('CT90_ms'), 1))
        self.lbl_rt50.setText(fmt(well_avg.get('RT50_ms'), 1))
        self.lbl_rt90.setText(fmt(well_avg.get('RT90_ms'), 1))
        self.lbl_cvel.setText(fmt(well_avg.get('Max_Contraction_Velocity'), 4))
        self.lbl_rvel.setText(fmt(well_avg.get('Max_Relaxation_Velocity'), 4))
        
        self.lbl_fa.setText(fmt(well_avg.get('Fractional_Anisotropy'), 3))
        self.lbl_angle.setText(f"{fmt(well_avg.get('Contraction_Angle_deg'), 1)}°" if pd.notnull(well_avg.get('Contraction_Angle_deg')) else "N/A")
        self.lbl_coherence.setText(fmt(well_avg.get('Vector_Coherence'), 3))
        
        self.lbl_amp_std.setText(fmt(well_std.get('Peak_Amplitude_au'), 2))
        self.lbl_ttp_std.setText(fmt(well_std.get('Time_to_Peak_ms'), 1))
        self.lbl_rt_std.setText(fmt(well_std.get('Relaxation_Time_ms'), 1))
        self.lbl_p2p_std.setText(fmt(well_std.get('Peak_to_Peak_Time_ms'), 1))
        
        self.lbl_ct50_std.setText(fmt(well_std.get('CT50_ms'), 1))
        self.lbl_ct90_std.setText(fmt(well_std.get('CT90_ms'), 1))
        self.lbl_rt50_std.setText(fmt(well_std.get('RT50_ms'), 1))
        self.lbl_rt90_std.setText(fmt(well_std.get('RT90_ms'), 1))
        self.lbl_cvel_std.setText(fmt(well_std.get('Max_Contraction_Velocity'), 4))
        self.lbl_rvel_std.setText(fmt(well_std.get('Max_Relaxation_Velocity'), 4))
        
        self.lbl_fa_std.setText(fmt(well_std.get('Fractional_Anisotropy'), 3))
        self.lbl_angle_std.setText(f"{fmt(well_std.get('Contraction_Angle_deg'), 1)}°" if pd.notnull(well_std.get('Contraction_Angle_deg')) else "N/A")
        self.lbl_coherence_std.setText(fmt(well_std.get('Vector_Coherence'), 3))
        
        self.draw_summary_wave(well_avg)
        self.draw_recorded_wave(None)

    def update_map(self):
        self.scatter.clear()
        for overlay in getattr(self, 'map_overlays', []):
            self.plot_item.removeItem(overlay)
        self.map_overlays = []
        self.field_curves = {}
        self.color_bar.hide()
        self.color_bar.blockSignals(True)
        self.color_bar.setLevels((0, 1))
        self.color_bar.blockSignals(False)
        self.plot_item.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        self.current_x_vals = self.current_y_vals = self.current_z_vals = np.array([])
        self.current_cell_ids = np.array([])
        self.current_row_ids = []
        self.scatter_points = []
        self.map_notice.clear()
        data = getattr(self, 'current_field_data', self.current_well_data)
        if data is None or data.empty:
            self.spatial_notice.clear(); self.well_cells.hide(); self.glw.show()
            self.summary_curve.clear(); self.summary_plot.hide(); self.cell_context.clear()
            self.summary_note.clear(); self.summary_note.hide()
            for index in range(self.metric_combo.count()):
                self.metric_combo.model().item(index).setEnabled(False)
            return
        metric = self.metric_combo.currentData()
        full_well = self.field_selector.currentData() is None
        self.fill_well_table(data, metric)
        self.well_cells.hide()
        self.glw.show()
        for index in range(self.metric_combo.count()):
            key = self.metric_combo.itemData(index)
            self.metric_combo.model().item(index).setEnabled(bool(key in data and data[key].notna().any()))
        if full_well:
            identities = set(data[['Recording_ID', 'FOV_ID']].itertuples(index=False, name=None))
            missing = identities - set(self.spatial_registrations)
            if missing:
                self.glw.hide(); self.well_cells.show()
                fields = ', '.join(f'Field {field} ({recording})' for recording, field in sorted(missing))
                reason = 'missing or incompatible' if self.spatial_warnings else 'missing'
                self.map_notice.setText(f'Full-well map unavailable: field positions are {reason}. {fields}. Select a field to view its map.')
                return
        if metric not in data:
            self.glw.hide()
            self.map_notice.setText('This dataset does not contain the selected measurement.')
            return
        if not data[metric].notna().any():
            self.glw.hide()
            self.map_notice.setText('No measured values are available for this metric.')
            return
        if full_well and self.spatial_registrations:
            placed = []
            for identity, document in self.spatial_registrations.items():
                recording, field = identity
                cells = data[(data.Recording_ID == recording) & (data.FOV_ID == field)].copy()
                cells = cells[np.isfinite(cells.Centroid_X) & np.isfinite(cells.Centroid_Y)]
                width, height = document['image_size_px']
                cells = cells[cells.Centroid_X.between(0, width) & cells.Centroid_Y.between(0, height)]
                if len(cells):
                    xy = transform_points(document, cells.Centroid_X, cells.Centroid_Y)
                    cells['Centroid_X'], cells['Centroid_Y'] = xy[:, 0], xy[:, 1]
                    placed.append(cells)
                outline = field_outline(document)
                overlay = pg.PlotDataItem(outline[:, 0], outline[:, 1], pen=pg.mkPen('#526174', width=1))
                overlay.setCurveClickable(True, width=10)
                overlay.sigClicked.connect(lambda _, event, key=identity: self.select_field_identity(key))
                self.plot_item.addItem(overlay); self.map_overlays.append(overlay)
                self.field_curves[identity] = overlay
            data = pd.concat(placed) if placed else data.iloc[:0]
            unplaced = len(self.current_well_data) - len(data)
            if self.spatial_registrations and unplaced:
                self.map_notice.setText(f'{unplaced} cells have no valid well placement; they remain in the cell list and well statistics.')
            self.plot_item.setLabel('left', 'Y', units='µm'); self.plot_item.setLabel('bottom', 'X', units='µm')
            self.plot_item.getViewBox().invertY(False)
            self.plot_item.getViewBox().setAspectLocked(True)
        else:
            data = data[np.isfinite(data['Centroid_X']) & np.isfinite(data['Centroid_Y'])]
            self.plot_item.setLabel('left', 'Y', units='px'); self.plot_item.setLabel('bottom', 'X', units='px')
            self.plot_item.getViewBox().invertY(False)
            self.plot_item.getViewBox().setAspectLocked(True)
        if data.empty:
            self.glw.hide()
            self.well_cells.show()
            if not self.map_notice.text():
                self.map_notice.setText('Spatial coordinates are unavailable. Select a cell to inspect its measurements.')
            return
        self.current_x_vals = data['Centroid_X'].to_numpy()
        self.current_y_vals = data['Centroid_Y'].to_numpy()
        self.current_z_vals = data[metric].to_numpy()
        self.current_cell_ids = data['Cell_ID'].to_numpy()
        self.current_row_ids = list(data.index)

        valid_mask = ~pd.isna(self.current_z_vals)
        self.color_bar.blockSignals(True) # Prevent recursive updates when setting levels programmatically
        if not np.any(valid_mask):
            self.color_bar.setLevels((0, 1))
        else:
            z_float = np.array(self.current_z_vals, dtype=float)
            min_z, max_z = np.nanmin(z_float), np.nanmax(z_float)
            if min_z == max_z:
                self.color_bar.setLevels((min_z - 0.1, max_z + 0.1))
            else:
                self.color_bar.setLevels((min_z, max_z))
        self.color_bar.blockSignals(False)
        self.color_bar.setVisible(bool(np.any(valid_mask)))
        self.plot_item.enableAutoRange()
        if not full_well:
            document = self.spatial_registrations.get(self.field_selector.currentData())
            width, height = document['image_size_px'] if document else (max(1., float(data.Centroid_X.max())), max(1., float(data.Centroid_Y.max())))
            self.plot_item.setRange(xRange=(0, width), yRange=(0, height), padding=.04)
        self.update_scatter_colors()

    def update_scatter_colors(self):
        """Dynamically updates the heatmap colors based on the interactive ColorBar slider levels."""
        if not hasattr(self, 'current_z_vals'): return
        
        levels = self.color_bar.levels()
        min_z, max_z = levels[0], levels[1]
        colormap = self.map_colormap
        
        self.scatter_points = []
        spots = []
        for i, z in enumerate(self.current_z_vals):
            spot_data = {"Cell_ID": self.current_cell_ids[i], "idx": self.current_row_ids[i]}
            self.scatter_points.append(spot_data)
            
            if pd.isna(z):
                brush = pg.mkBrush(color=(100, 100, 100, 150))
            else:
                # Clamp values to the slider bounds and calculate dynamic color
                norm_z = 0.5 if max_z == min_z else max(0.0, min(1.0, (float(z) - min_z) / (max_z - min_z)))
                brush = pg.mkBrush(colormap.map(norm_z, 'qcolor'))
                
            spots.append({'pos': (self.current_x_vals[i], self.current_y_vals[i]), 'data': spot_data, 'brush': brush})
            
        self.scatter.setData(spots)

    def on_point_clicked(self, item, points):
        if not points: return
        
        self.show_cell(int(points[0].data()['idx']))

    def show_cell(self, index):
        cell_data = self.current_well_data.loc[index]
        cell_std = self.current_well_std.loc[index]
        cell_id = cell_data['Cell_ID']
        # Update Header
        self.lbl_target.setText(display_cell_name(cell_id, cell_data.get('Object_Type', 'cell')))
        self.cell_context.setText(f"Field {cell_data['FOV_ID']} · Recording {cell_data['Recording_ID']}")
        self.cell_selector.blockSignals(True)
        self.cell_selector.setCurrentIndex(self.cell_selector.findData(index))
        self.cell_selector.blockSignals(False)
        
        def fmt(val, decimals=2):
            return f"{val:.{decimals}f}" if pd.notnull(val) else "N/A"

        # Update Core Stats
        self.lbl_amp.setText(fmt(cell_data.get('Peak_Amplitude_au'), 2))
        self.lbl_ttp.setText(fmt(cell_data.get('Time_to_Peak_ms'), 1))
        self.lbl_rt.setText(fmt(cell_data.get('Relaxation_Time_ms'), 1))
        self.lbl_p2p.setText(fmt(cell_data.get('Peak_to_Peak_Time_ms'), 1))
        
        # Update Advanced Stats
        self.lbl_ct50.setText(fmt(cell_data.get('CT50_ms'), 1))
        self.lbl_ct90.setText(fmt(cell_data.get('CT90_ms'), 1))
        self.lbl_rt50.setText(fmt(cell_data.get('RT50_ms'), 1))
        self.lbl_rt90.setText(fmt(cell_data.get('RT90_ms'), 1))
        self.lbl_cvel.setText(fmt(cell_data.get('Max_Contraction_Velocity'), 4))
        self.lbl_rvel.setText(fmt(cell_data.get('Max_Relaxation_Velocity'), 4))
        
        self.lbl_fa.setText(fmt(cell_data.get('Fractional_Anisotropy'), 3))
        self.lbl_angle.setText(f"{fmt(cell_data.get('Contraction_Angle_deg'), 1)}°" if pd.notnull(cell_data.get('Contraction_Angle_deg')) else "N/A")
        self.lbl_coherence.setText(fmt(cell_data.get('Vector_Coherence'), 3))
        
        # Update Std Dev Stats
        self.lbl_amp_std.setText(fmt(cell_std.get('Peak_Amplitude_au'), 2))
        self.lbl_ttp_std.setText(fmt(cell_std.get('Time_to_Peak_ms'), 1))
        self.lbl_rt_std.setText(fmt(cell_std.get('Relaxation_Time_ms'), 1))
        self.lbl_p2p_std.setText(fmt(cell_std.get('Peak_to_Peak_Time_ms'), 1))
        
        self.lbl_ct50_std.setText(fmt(cell_std.get('CT50_ms'), 1))
        self.lbl_ct90_std.setText(fmt(cell_std.get('CT90_ms'), 1))
        self.lbl_rt50_std.setText(fmt(cell_std.get('RT50_ms'), 1))
        self.lbl_rt90_std.setText(fmt(cell_std.get('RT90_ms'), 1))
        self.lbl_cvel_std.setText(fmt(cell_std.get('Max_Contraction_Velocity'), 4))
        self.lbl_rvel_std.setText(fmt(cell_std.get('Max_Relaxation_Velocity'), 4))
        
        self.lbl_fa_std.setText(fmt(cell_std.get('Fractional_Anisotropy'), 3))
        self.lbl_angle_std.setText(f"{fmt(cell_std.get('Contraction_Angle_deg'), 1)}°" if pd.notnull(cell_std.get('Contraction_Angle_deg')) else "N/A")
        self.lbl_coherence_std.setText(fmt(cell_std.get('Vector_Coherence'), 3))
        
        self.draw_summary_wave(cell_data)
        self.draw_recorded_wave(cell_data)

    def draw_summary_wave(self, data):
        self.summary_curve.clear(); self.summary_plot.hide()
        self.summary_note.clear(); self.summary_note.hide()
        try:
            ttp, rt, amplitude = [float(data.get(key)) for key in ('Time_to_Peak_ms', 'Relaxation_Time_ms', 'Peak_Amplitude_au')]
            if not np.isfinite([ttp, rt, amplitude]).all() or ttp <= 0 or rt <= 0 or amplitude < 0:
                raise ValueError('Missing or invalid summary measurements.')
        except (TypeError, ValueError, AttributeError):
            self.summary_note.setText('Illustrative beat unavailable: amplitude, time to peak and relaxation time are required.')
            self.summary_note.show()
            return
        self.summary_curve.setData([0, ttp, ttp + rt, ttp + rt + 200], [0, amplitude, 0, 0])
        self.summary_plot.show(); self.summary_plot.autoRange()

    def draw_recorded_wave(self, cell_data):
        self._trace_row = cell_data
        self.wave_note.clear(); self.wave_note.show()
        self.wave_curve.clear(); self.mini_plot.hide()
        self.mini_plot.getPlotItem().setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        self.trace_signal.setEnabled(cell_data is not None)
        self.trace_signal.setVisible(bool(cell_data is not None and isinstance(cell_data.get('Trace_File'), str) and cell_data.get('Trace_File')))
        self.grp_wave.show()
        if cell_data is None:
            self.wave_note.setText('Select a cell to view its recorded signal.')
            return
        try:
            signal = self.trace_signal.currentText()
            x, y = read_trace(getattr(self, 'dataset_path', ''), cell_data, signal)
            self.wave_curve.setData(x, y)
            self.mini_plot.setLabel('bottom', 'Time', units='ms')
            quantity, units = {'Contraction (Raw a.u.)': ('Raw contraction', 'a.u.'),
                'Contraction (Area-Norm a.u.)': ('Area-normalized contraction', 'a.u.'),
                'Contraction (Fractional dI/I0)': ('Fractional intensity change', 'dI/I0'),
                'Speed (a.u.)': ('Speed', 'a.u.')}[signal]
            if quantity == 'Speed':
                self.mini_plot.setLabel('left', quantity, units=units)
            else:
                self.mini_plot.setLabel('left', quantity.replace('-', '<br>').replace(' ', '<br>') + f'<br>({units})')
            self.mini_plot.fit_label_height(self.signal_plot_height)
            self.wave_note.hide()
            self.mini_plot.show(); self.mini_plot.autoRange()
        except (OSError, ValueError, KeyError) as exc:
            if not isinstance(cell_data.get('Trace_File'), str) or not cell_data.get('Trace_File'):
                self.wave_note.setText('Recorded signal unavailable: no trace was saved with these results.')
            else:
                self.wave_note.setText(f'Recorded signal unavailable. {exc}')



SETTING_GUIDANCE = {
    ('capture', 'target_fps'): 'Start with 100 FPS. Use 200–226 FPS only when the camera and exposure can sustain the higher rate.',
    ('annotator', 'confidence_threshold'): 'Start with 0.15. Increase it if debris is detected as cells; check faint cells before lowering it.',
    ('annotator', 'nms_iou_threshold'): 'Start with 0.40. Increase it only if distinct overlapping cells are being removed.',
    ('scout', 'settle_dwell_ms'): 'Start with 40 ms; increase the wait if images show movement after the stage stops.',
    ('scout', 'scout_duration_sec'): 'Start with 15 seconds; longer recordings capture more beats and use more storage.',
    ('analyzer', 'default_metric'): 'Start with area-normalized amplitude. Use fractional intensity change when comparing relative brightness changes.',
    ('kinetics', 'enable_asls_detrending'): 'Keep the default enabled to remove slow baseline drift.',
    ('kinetics', 'asls_lambda'): 'Start with 100,000. Review the recorded signal when changing baseline smoothing.',
    ('kinetics', 'asls_p'): 'Start with 0.001 to keep the estimated baseline close to the resting signal.',
    ('kinetics', 'lowpass_cutoff_hz'): 'Start with 18 Hz; inspect the measured trace before changing the noise cutoff.',
    ('kinetics', 'savgol_window_pct'): 'Start with a 7% smoothing window; check that brief changes remain visible.',
    ('kinetics', 'peak_prominence_pct'): 'Start with a 30% threshold; lower values can count small fluctuations as beats.',
}

class SettingsTab(QWidget):
    settings_saved = pyqtSignal()
    workspace_changed = pyqtSignal()
    account_changed = pyqtSignal()
    """Preference editors with validation and status from the last saved snapshot."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.widgets = {}
        self._shared_admin = False
        self._shared_admin_token = None
        self._shared_generation = 0
        self._shared_pending_restart = bool(getattr(tempo_preferences, '_pending_workspace', None))
        self._shared_published = None
        self._shared_updating = False
        self._shared_local_save_pending = False
        self.setup_ui()
        self._saved_values = self.preference_values()
        for widget in self.widgets.values():
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self.update_save_state)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self.update_save_state)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self.update_save_state)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.update_save_state)

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        header = QHBoxLayout()
        self.lbl_save_status = QLabel('No changes')
        self.lbl_save_status.setWordWrap(True)
        self.lbl_save_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.lbl_save_status, 1)
        self.btn_tools = QPushButton('Application Tools')
        self.btn_tools.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        header.addWidget(self.btn_tools)
        main_layout.addLayout(header)

        # Scroll area hosting the preference categories.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.settings_scroll = scroll

        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(10)
        scroll_layout.setContentsMargins(10, 10, 10, 10)

        category_titles = {
            "appearance": "Appearance",
            "capture": "Optical Acquisition",
            "annotator": "Annotation",
            "collaboration": "Account and Shared Data",
            "scout": "Viability Screening",
            "cruncher": "Kinematics Extraction",
            "analyzer": "Statistical Analysis and Quality Control",
            "kinetics": "Signal Filtering",
            "pacing": "Pacing"
        }

        all_prefs = tempo_preferences.get_all()
        hidden_settings = {
            'capture': {'camera_profile', 'bit_depth', 'circular_buffer_mb', 'pre_trigger_buffer_ms', 'panel_widths'},
            'annotator': {'auto_clear_on_annotate', 'active_model'},
            'collaboration': {'is_maintainer', 'lock_ttl_days'},
            'cruncher': {'in_memory_streaming'},  # Extraction streams frames regardless of this unused preference.
        }
        label_width = max(self.fontMetrics().horizontalAdvance(spec.get('label', key))
                          for category, values in all_prefs.items() if category in category_titles
                          for key, spec in values.items() if key not in hidden_settings.get(category, set())) + 4
        for cat_key, cat_title in category_titles.items():
            if cat_key not in all_prefs:
                continue
            cat_data = all_prefs[cat_key]

            group_box = QGroupBox(cat_title)
            group_layout = QVBoxLayout(group_box)
            group_layout.setSpacing(6)
            group_layout.setContentsMargins(10, 8, 10, 10)
            if cat_key == 'collaboration':
                self.shared_settings_status = QLabel()
                self.shared_settings_status.setWordWrap(True)
                self.shared_settings_status.hide()
                group_layout.addWidget(self.shared_settings_status)
                self.shared_settings_error = ErrorDetails()
                group_layout.addWidget(self.shared_settings_error)
                shared_actions = FlowRow()
                self.btn_refresh_shared = QPushButton('Refresh Shared Settings')
                self.btn_refresh_shared.clicked.connect(self.refresh_shared_settings)
                self.btn_update_shared = QPushButton('Update Shared Settings')
                self.btn_update_shared.setProperty('primary', True)
                self.btn_update_shared.clicked.connect(self.update_shared_settings)
                self.btn_update_shared.hide()
                shared_actions.flow.addWidget(self.btn_refresh_shared)
                shared_actions.flow.addWidget(self.btn_update_shared)

            for item_key, item_spec in cat_data.items():
                if item_key in hidden_settings.get(cat_key, set()):
                    continue
                item_container = QWidget()
                item_layout = QVBoxLayout(item_container)
                item_layout.setContentsMargins(2, 2, 2, 4)
                item_layout.setSpacing(2)

                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 0, 0, 0)

                label_text = item_spec.get("label", item_key)
                val = item_spec.get("value")

                lbl_name = QLabel(label_text)
                lbl_name.setWordWrap(True)
                row_layout.addWidget(lbl_name)

                if isinstance(val, bool):
                    chk = QCheckBox("Enabled" if val else "Disabled")
                    chk.setChecked(val)
                    chk.toggled.connect(lambda checked, c=chk: c.setText("Enabled" if checked else "Disabled"))
                    row_layout.addWidget(chk)
                    self.widgets[(cat_key, item_key)] = chk
                elif "options" in item_spec:
                    combo = QComboBox()
                    combo.setMinimumWidth(140)
                    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
                    for opt in item_spec["options"]:
                        combo.addItem(item_spec.get("option_labels", {}).get(str(opt), str(opt)), opt)
                    combo.setCurrentIndex(combo.findData(val))
                    row_layout.addWidget(combo)
                    self.widgets[(cat_key, item_key)] = combo
                elif isinstance(val, float):
                    dspin = QDoubleSpinBox()
                    from decimal import Decimal
                    precision_values = [val, item_spec.get('min', val), item_spec.get('step', 0.1)]
                    decimals = max(2, *(max(0, -Decimal(str(number)).as_tuple().exponent) for number in precision_values))
                    dspin.setDecimals(max(int(item_spec.get('decimals', 0)), decimals))
                    low, high = float(item_spec.get('min', 0.0)), float(item_spec.get('max', 10000000.0))
                    dspin.setRange(min(low, val), max(high, val))
                    dspin.setProperty('settingMinimum', low); dspin.setProperty('settingMaximum', high)
                    dspin.setSingleStep(float(item_spec.get("step", 0.1)))
                    dspin.setValue(val)
                    dspin.setMinimumWidth(120)
                    row_layout.addWidget(dspin)
                    self.widgets[(cat_key, item_key)] = dspin
                elif isinstance(val, int):
                    spin = QSpinBox()
                    low, high = int(item_spec.get('min', 0)), int(item_spec.get('max', 10000000))
                    spin.setRange(min(low, val), max(high, val))
                    spin.setProperty('settingMinimum', low); spin.setProperty('settingMaximum', high)
                    spin.setValue(val)
                    spin.setMinimumWidth(120)
                    row_layout.addWidget(spin)
                    self.widgets[(cat_key, item_key)] = spin
                else:
                    if (cat_key, item_key) == ('collaboration', 'collaborator_id'):
                        val = safe_profile_name(val, tempo_hub.token)
                    line = QLineEdit(str(val))
                    line.setMinimumWidth(200)
                    if (cat_key, item_key) == ('capture', 'data_folder'):
                        from tempo_config import get_data_dir
                        line.setPlaceholderText(get_data_dir(''))
                    if (cat_key, item_key) == ('collaboration', 'collaborator_id'):
                        line.textChanged.connect(self._guard_profile_name)
                    row_layout.addWidget(line)
                    self.widgets[(cat_key, item_key)] = line

                # Keep control sizing natural, aligned, and responsive to available width.
                control = self.widgets[(cat_key, item_key)]
                from tempo_shared_settings import SHARED_KEYS
                if cat_key == 'collaboration' and item_key in SHARED_KEYS:
                    control.setReadOnly(True)
                    control.setToolTip('Only the administrator can edit this.')
                row_layout.removeWidget(lbl_name); row_layout.removeWidget(control)
                row_widget.deleteLater()
                presentation_control = control
                if (cat_key, item_key) == ('capture', 'data_folder'):
                    choose = QPushButton('Choose…')
                    choose.clicked.connect(self.choose_data_folder)
                    presentation_control = FilePickerRow(control, choose)
                responsive_row = SettingRow(lbl_name, presentation_control, label_width)
                item_layout.addWidget(responsive_row)
                notes = QWidget()
                notes_layout = QVBoxLayout(notes)
                notes_layout.setContentsMargins(0, 0, 0, 0)
                notes_layout.setSpacing(2)

                # Subtext 1: Description with readable tech-cyan indicator color
                desc_text = item_spec.get("description", "") if cat_key != 'collaboration' else ''
                if desc_text:
                    desc_lbl = QLabel(desc_text)
                    desc_lbl.setStyleSheet(f'color: {DESCRIPTION_BLUE};')
                    desc_lbl.setWordWrap(True)
                    notes_layout.addWidget(desc_lbl)
                    font = desc_lbl.font(); font.setItalic(True); desc_lbl.setFont(font)

                guidance = SETTING_GUIDANCE.get((cat_key, item_key))
                if guidance:
                    rec_lbl = QLabel(guidance)
                    rec_lbl.setWordWrap(True)
                    rec_lbl.setStyleSheet(f'color: {GUIDANCE_MINT};')
                    font = rec_lbl.font(); font.setItalic(True); rec_lbl.setFont(font)
                    notes_layout.addWidget(rec_lbl)

                if notes_layout.count():
                    responsive_row.setNotes(notes)
                else:
                    notes.deleteLater()

                group_layout.addWidget(item_container)
                if cat_key == 'collaboration' and item_key == 'hf_models_repo':
                    group_layout.addWidget(shared_actions)

            scroll_layout.addWidget(group_box)

        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        main_layout.addWidget(scroll)

        # Bottom Controls (Save / Reset / Wizards)
        button_row = FlowRow()
        btn_bar = button_row.flow
        self.btn_save = QPushButton("Save Personal Settings")
        self.btn_save.setProperty('primary', True)
        self.btn_save.clicked.connect(self.save_preferences)

        self.btn_reset = QPushButton("Restore Defaults")
        set_button_color(self.btn_reset, '#4A5568')
        self.btn_reset.clicked.connect(self.reset_defaults)

        self.tools_menu = ContainedToolsMenu(self.btn_tools, [
            ('Update Models', self.sync_model_weights),
            ('Check Software Updates', self.check_software_updates),
            ('About TEMPO', self.show_application_details),
            ('Account Setup', self.run_account_setup),
            ('Guided Setup', self.run_guided_setup),
            ('Uninstall TEMPO...', lambda: self.run_uninstall())])
        self.btn_tools.clicked.connect(self.tools_menu.toggle)
        if getattr(tempo_preferences, 'load_error', ''):
            self.lbl_save_status.setText('Saved settings could not be loaded. Defaults are shown; review them before saving. ' + tempo_preferences.load_error)

        btn_bar.addWidget(self.btn_save)
        btn_bar.addWidget(self.btn_reset)
        main_layout.addWidget(button_row)

    def preference_values(self):
        values = {}
        for key, widget in self.widgets.items():
            if isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentData() if widget.currentData() is not None else widget.currentText()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                value = widget.text()
            values[key] = value
        return values

    def update_save_state(self, *_):
        self._update_shared_action()
        if self._shared_admin and self._shared_dirty():
            self._set_shared_status('Unsaved shared settings')
        elif self.shared_settings_status.text() == 'Unsaved shared settings':
            self._set_shared_status('Restart TEMPO to use the updated repositories.' if self._shared_pending_restart else '')
        if getattr(self, '_profile_name_rejected', False):
            self.lbl_save_status.setText('Access tokens and account IDs cannot be used as profile names. Enter your profile or laboratory name.')
            return
        if hasattr(self, '_saved_values'):
            values = self.personal_preference_values()
            self.lbl_save_status.setText('Unsaved changes' if any(value != self._saved_values.get(key) for key, value in values.items()) else 'No changes')

    def personal_preference_values(self):
        from tempo_shared_settings import SHARED_KEYS
        return {key: value for key, value in self.preference_values().items()
                if key[0] != 'collaboration' or key[1] not in SHARED_KEYS}

    def _shared_dirty(self):
        from tempo_shared_settings import SHARED_KEYS
        return hasattr(self, '_saved_values') and any(
            self.widgets[('collaboration', key)].text() != self._saved_values.get(('collaboration', key))
            for key in SHARED_KEYS)

    def _update_shared_action(self):
        verified = bool(self._shared_admin and self._shared_admin_token and self._shared_admin_token == tempo_hub.token)
        self.btn_update_shared.setVisible(verified)
        self.btn_update_shared.setEnabled(verified and not self._shared_updating and (
            self._shared_dirty() or self._shared_published is not True or self._shared_local_save_pending))

    def _guard_profile_name(self, value):
        from PyQt6.QtCore import QSignalBlocker
        self._profile_name_rejected = bool(value) and safe_profile_name(value, tempo_hub.token) != value
        if self._profile_name_rejected:
            field = self.widgets[('collaboration', 'collaborator_id')]
            with QSignalBlocker(field):
                field.clear()
            self.update_save_state()

    def save_preferences(self):
        from tempo_shared_settings import SHARED_KEYS, values
        field = self.widgets.get(('collaboration', 'collaborator_id'))
        if getattr(self, '_profile_name_rejected', False) or field is not None and safe_profile_name(field.text(), tempo_hub.token) != field.text():
            self.lbl_save_status.setText('Settings were not saved. Enter a profile or laboratory name, not an access token or account ID.')
            return
        invalid = []
        for (category, key), widget in self.widgets.items():
            if isinstance(widget, (QSpinBox, QDoubleSpinBox)) and widget.property('settingMinimum') is not None:
                if not widget.property('settingMinimum') <= widget.value() <= widget.property('settingMaximum'):
                    invalid.append(tempo_preferences.get_all()[category][key].get('label', key))
        if invalid:
            self.lbl_save_status.setText('Settings were not saved. Correct the value for: ' + ', '.join(invalid))
            return
        candidate = copy.deepcopy(tempo_preferences.get_all())
        personal = self.personal_preference_values()
        for (cat_key, item_key), val in personal.items():
            candidate[cat_key][item_key]['value'] = val
        shared = getattr(tempo_preferences, '_pending_workspace', None) or values()
        for key in SHARED_KEYS:
            candidate['collaboration'][key]['value'] = shared[key]
        profile_changed = candidate['collaboration']['collaborator_id']['value'] != tempo_preferences.get('collaboration', 'collaborator_id')
        if tempo_preferences.save_to_disk(candidate):
            # Apply only after the complete document has been saved successfully.
            for (cat_key, item_key), val in personal.items():
                tempo_preferences.set(cat_key, item_key, val)
            self._saved_values.update(personal)
            self.lbl_save_status.setText('Settings saved')
            if (profile_changed or getattr(self, '_profile_publish_pending', False)) and tempo_hub.get_collaborator_id() != 'unverified_account':
                self._profile_publish_pending = True
                try:
                    run_task(self, 'Updating your shared profile name…', tempo_hub.publish_collaborator_profile)
                    self._profile_publish_pending = False
                except Exception as error:
                    self.lbl_save_status.setText('Settings saved locally. Your shared display name could not be updated; save again to retry. ' + redact_credentials(str(error), tempo_hub.token))
            self.settings_saved.emit()
        else:
            self.lbl_save_status.setText('Settings could not be saved. Check folder permissions and try again.')

    def choose_data_folder(self):
        from tempo_config import get_data_dir
        field = self.widgets[('capture', 'data_folder')]
        folder = QFileDialog.getExistingDirectory(self, 'Choose Data Folder', get_data_dir(field.text()))
        if folder:
            field.setText(folder)

    def update_shared_settings(self):
        from tempo_shared_settings import SHARED_KEYS, validate_settings, publish_shared_settings, apply_shared_settings
        if self._shared_updating:
            return
        token = tempo_hub.token
        self.shared_settings_error.clear()
        if not self._shared_admin or not token or self._shared_admin_token != token:
            self.lock_shared_fields()
            self._set_shared_status('Only the administrator can change shared settings.')
            return
        try:
            shared = validate_settings({key: self.widgets[('collaboration', key)].text() for key in SHARED_KEYS})
        except ValueError as error:
            self.shared_settings_error.set_error('Shared settings were not updated.', error, secrets=(token,))
            return
        if not confirm_action(self, 'Update shared settings?',
                'These changes apply to all collaborators at their next launch. Restart TEMPO here after changing repositories.',
                'Update Shared Settings'):
            return
        self._shared_generation += 1
        self._shared_updating = True
        self.btn_refresh_shared.setEnabled(False)
        self._update_shared_action()
        try:
            try:
                record = run_task(self, 'Updating shared settings…', lambda: publish_shared_settings(
                    tempo_hub, shared, previous=getattr(self, '_shared_baseline', None)))
            except Exception as error:
                self.shared_settings_error.set_error('Could not update shared settings.', error, secrets=(token, tempo_hub.token))
                return
            self._shared_baseline = dict(record['settings'])
            self._shared_published = True
            self._shared_local_save_pending = True
            if token != tempo_hub.token:
                self.lock_shared_fields()
                self._set_shared_status('Shared settings were updated. Refresh after signing in to use them here.')
                return
            try:
                self._shared_pending_restart = apply_shared_settings(record, live=True)
            except Exception as error:
                self.shared_settings_error.set_error('Shared settings were updated, but could not be saved on this computer.', error, secrets=(token,))
                return
            self._shared_local_save_pending = False
            for key, value in record['settings'].items():
                widget = self.widgets[('collaboration', key)]
                with QSignalBlocker(widget):
                    widget.setText(value)
                self._saved_values[('collaboration', key)] = value
            self._set_shared_status('Shared settings updated. Restart TEMPO to use the new repositories.' if self._shared_pending_restart else 'Shared settings updated')
            self.workspace_changed.emit()
        finally:
            self._shared_updating = False
            self.btn_refresh_shared.setEnabled(True)
            self._update_shared_action()

    def _set_shared_status(self, text):
        self.shared_settings_status.setText(text)
        self.shared_settings_status.setVisible(bool(text))

    def lock_shared_fields(self):
        self._set_shared_access(False, None)

    def focus_notebook_url(self):
        field = self.widgets[('collaboration', 'colab_notebook_url')]
        self.settings_scroll.ensureWidgetVisible(field, 0, 24)
        field.setFocus(Qt.FocusReason.OtherFocusReason)
        field.selectAll()
        if field.isReadOnly():
            self.refresh_shared_settings()

    def _set_shared_access(self, admin, token):
        from tempo_shared_settings import SHARED_KEYS
        self._shared_admin = bool(admin and token and token == tempo_hub.token)
        self._shared_admin_token = token if self._shared_admin else None
        for key in SHARED_KEYS:
            self.widgets[('collaboration', key)].setReadOnly(not self._shared_admin)
            self.widgets[('collaboration', key)].setToolTip('' if self._shared_admin else 'Only the administrator can edit this.')
        self._update_shared_action()

    def refresh_shared_settings(self):
        from tempo_shared_settings import fetch_shared_settings, apply_shared_settings, is_workspace_admin, SHARED_KEYS
        from PyQt6 import sip
        if self._shared_updating:
            return
        self._shared_generation += 1
        generation = self._shared_generation
        self.lock_shared_fields()
        self.shared_settings_error.clear()
        if not tempo_hub.token:
            self._set_shared_status('Sign in to refresh shared settings.')
            return
        token = tempo_hub.token
        self._set_shared_status('Checking shared settings…')
        def check():
            admin = is_workspace_admin(tempo_hub)
            try:
                return admin, fetch_shared_settings(tempo_hub), None
            except Exception as error:
                return admin, None, error
        def ready(result, error):
            if sip.isdeleted(self) or generation != self._shared_generation or token != tempo_hub.token:
                return
            if error:
                self._set_shared_status('')
                self.shared_settings_error.set_error('Could not verify the account.', error, secrets=(token,))
                return
            admin, record, read_error = result
            self._set_shared_access(admin, token)
            if read_error:
                self._set_shared_status('')
                self.shared_settings_error.set_error('Could not refresh shared settings. Using saved settings.', read_error, secrets=(token,))
                return
            self._shared_published = record is not None
            if any(self.widgets[('collaboration', key)].text() != self._saved_values.get(('collaboration', key)) for key in SHARED_KEYS):
                self._set_shared_status('Unsaved shared settings' if admin else '')
                self._update_shared_action()
                return
            try:
                if record is not None:
                    self._shared_pending_restart = apply_shared_settings(record, live=True)
            except Exception as error:
                self._set_shared_status('')
                self.shared_settings_error.set_error('Could not save the updated settings on this computer.', error, secrets=(token,))
                return
            if record is not None:
                self._shared_baseline = dict(record['settings'])
                for key, value in record['settings'].items():
                    field = self.widgets[('collaboration', key)]
                    field.blockSignals(True); field.setText(value); field.blockSignals(False)
                    self._saved_values[('collaboration', key)] = value
            self._set_shared_status('Restart TEMPO to use the updated repositories.' if self._shared_pending_restart else
                ('' if admin else 'Only the administrator can change shared settings.' if record is not None else 'Shared settings are not available yet.'))
            self._update_shared_action()
            self.workspace_changed.emit()
        run_background(check, ready)

    def reset_defaults(self):
        from tempo_config import DEFAULT_PREFERENCES
        from tempo_ui import confirm_action
        if not confirm_action(self, 'Restore default settings?',
                'Reset acquisition, annotation, analysis, and pacing settings. Account and repository settings are retained. Save to apply the defaults.',
                'Load Defaults'):
            return
        for (cat_key, item_key), widget in self.widgets.items():
            if cat_key == 'collaboration':
                continue
            val = DEFAULT_PREFERENCES[cat_key][item_key]['value']
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(val))
            elif isinstance(widget, QComboBox):
                index = widget.findData(val)
                if index >= 0:
                    widget.setCurrentIndex(index)
                else:
                    widget.setCurrentText(str(val))
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.setValue(val)
            elif isinstance(widget, QLineEdit):
                widget.setText(str(val))
        self.update_save_state()

    def show_application_details(self):
        from tempo_logo import AboutTempoDialog
        AboutTempoDialog(self, application_path=__file__).exec()

    def check_software_updates(self):
        from update_wizard import SoftwareUpdateWizard
        SoftwareUpdateWizard(self).exec()

    def sync_model_weights(self):
        from update_wizard import ModelWeightsUpdateWizard
        ModelWeightsUpdateWizard(self).exec()

    def run_account_setup(self):
        from setup_wizard import SetupWizardDialog
        dialog = SetupWizardDialog(self, account_only=True)
        dialog.account_saved.connect(self.on_account_saved)
        dialog.exec()

    def on_account_saved(self):
        self.refresh_account_fields()
        self.refresh_shared_settings()
        self.account_changed.emit()

    def refresh_account_fields(self):
        field = self.widgets.get(('collaboration', 'collaborator_id'))
        if field is not None:
            field.setText(safe_profile_name(tempo_preferences.get('collaboration', 'collaborator_id'), tempo_hub.token))
            self._saved_values[('collaboration', 'collaborator_id')] = field.text()
            self.update_save_state()

    def run_guided_setup(self):
        from setup_wizard import SetupWizardDialog
        dialog = SetupWizardDialog(self)
        dialog.account_saved.connect(self.on_account_saved)
        dialog.exec()

    def run_uninstall(self):
        from tempo_uninstall import UninstallTempoDialog
        dialog = UninstallTempoDialog(self.window() or self)
        dialog.exec()


# --- 3. The Main Dashboard Window ---
class TempoDashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        from tempo_ui import install_ui
        install_ui()
        self.setWindowTitle("TEMPO")
        self.setGeometry(100, 100, 960, 620)

        # Set Window Icon
        icon_path = application_icon_path()
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(str(icon_path)))

        # Main Central Container
        central_container = QWidget()
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(8, 6, 8, 8)
        central_layout.setSpacing(6)
        self.setCentralWidget(central_container)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        central_layout.addWidget(self.tabs)
        self.account_indicator = QPushButton('Checking account…')
        self.account_indicator.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.account_indicator.setAccessibleName('Account status and setup')
        self.account_indicator.clicked.connect(lambda: self.tab_settings.run_account_setup())
        account_slot = QWidget()
        account_layout = QHBoxLayout(account_slot)
        account_layout.setContentsMargins(6, 2, 2, 2)
        account_layout.addWidget(self.account_indicator)
        self.tabs.setCornerWidget(account_slot, Qt.Corner.TopRightCorner)
        self._account_generation = 0

        from tempo_startup_diagnostics import record as startup_stage
        startup_stage('dashboard.capture.started')
        self.tab_capture = CaptureTab()
        startup_stage('dashboard.capture.completed')
        self.tab_capture.cleanup_finished.connect(self.capture_cleanup_finished)
        self.tab_annotator = AnnotatorTab()
        self.tab_cruncher = QWidget()
        self.tab_analyzer = AnalyzerTab()
        self.tab_settings = SettingsTab()
        self.tab_settings.settings_saved.connect(self.tab_analyzer.apply_display_preferences)
        self.tab_settings.settings_saved.connect(tempo_backend.apply_data_folder)
        
        self.tabs.addTab(self.tab_capture, "Acquisition")
        self.tabs.addTab(self.tab_annotator, "Segmentation Annotation")
        self.tabs.addTab(self.tab_cruncher, "Kinematics Extraction")
        self.tabs.addTab(self.tab_analyzer, "Physiological Analysis")
        self.tabs.addTab(self.tab_settings, "Settings")
        from maintainer_tab import MaintainerTab
        self.tab_maintainer = MaintainerTab(self.tab_annotator, self)
        self.tabs.addTab(self.tab_maintainer, 'Maintainer')
        self.tab_maintainer.access_checked.connect(lambda allowed:self.tabs.setTabVisible(self.tabs.indexOf(self.tab_maintainer), allowed))
        self.tab_settings.settings_saved.connect(self.tab_annotator.refresh_account_identity)
        self.tab_settings.account_changed.connect(self.tab_annotator.refresh_account_identity)
        self.tab_settings.account_changed.connect(self.tab_maintainer.verify_access)
        self.tab_settings.workspace_changed.connect(self.tab_maintainer.refresh_notebook_link)
        self.tab_maintainer.notebook_settings_requested.connect(self.edit_notebook_url)
        self.tab_maintainer.account_changed.connect(self.tab_settings.refresh_account_fields)
        self.tab_maintainer.account_changed.connect(self.tab_annotator.refresh_account_identity)
        self.tab_settings.account_changed.connect(self.refresh_account_indicator)
        self.tab_maintainer.account_changed.connect(self.refresh_account_indicator)
        self.tab_maintainer.access_checked.connect(lambda _: self.refresh_account_indicator())

        self.setup_cruncher_tab()
        from tempo_activity_icon import ActivityIconController
        self.activity_icon = ActivityIconController(QApplication.instance(), self)
        self.tab_settings.settings_saved.connect(self.activity_icon.apply_preferences)
        self.activity_icon.apply_preferences()
        QTimer.singleShot(0, self.refresh_account_indicator)

    def edit_notebook_url(self):
        self.tabs.setCurrentWidget(self.tab_settings)
        self.tab_settings.focus_notebook_url()

    def refresh_account_indicator(self):
        self._account_generation += 1
        generation = self._account_generation
        token = tempo_hub.token
        loading_credentials = not tempo_hub._credentials_ready
        settings = getattr(self, 'tab_settings', None)
        if settings is not None and getattr(settings, '_shared_admin_token', None) != token:
            settings.lock_shared_fields()
        self.account_indicator.setAccessibleDescription('')
        if not token and not loading_credentials:
            self.account_indicator.setText('Signed out')
            self.account_indicator.setToolTip('Open Account Setup to sign in.')
            return
        self.account_indicator.setText('Checking account…')
        expected = {'token': token}
        def check_account():
            tempo_hub.ensure_credentials()
            expected['token'] = tempo_hub.token
            return tempo_hub.authenticated_account(force_refresh=True) if tempo_hub.token else None
        def ready(identity, error):
            from PyQt6 import sip
            if sip.isdeleted(self) or generation != self._account_generation or expected['token'] != tempo_hub.token:
                return
            if error:
                if isinstance(error, CredentialMigrationRequired):
                    try:
                        run_task(self, 'Securing saved account…', lambda: tempo_hub.ensure_credentials(migrate=True))
                    except Exception:
                        self.account_indicator.setText('Account storage unavailable')
                        self.account_indicator.setToolTip(tempo_hub.credential_error or 'The saved account could not be migrated. Open Account Setup to retry.')
                    else:
                        maintainer = getattr(self, 'tab_maintainer', None)
                        if maintainer is not None:
                            maintainer.verify_access()
                        self.refresh_account_indicator()
                    return
                self.account_indicator.setText('Account storage unavailable' if tempo_hub.credential_error else 'Account not verified')
                self.account_indicator.setToolTip(tempo_hub.credential_error or 'Account verification failed. Open Account Setup to reconnect.')
            elif not identity:
                self.account_indicator.setText('Signed out')
                self.account_indicator.setToolTip('Open Account Setup to sign in.')
            else:
                name = tempo_hub.get_account_display_name()
                available = max(160, min(280, self.width() // 3))
                text = self.account_indicator.fontMetrics().elidedText('Account: ' + name, Qt.TextElideMode.ElideRight, available - 24)
                self.account_indicator.setText(text)
                self.account_indicator.setAccessibleDescription(f'Signed in as {name}. Open Account Setup.')
                self.account_indicator.setToolTip(f'Signed in as {name}')
                settings = getattr(self, 'tab_settings', None)
                if settings is not None:
                    settings.refresh_shared_settings()
                annotator = getattr(self, 'tab_annotator', None)
                if annotator is not None:
                    annotator.refresh_account_identity()
                if loading_credentials:
                    maintainer = getattr(self, 'tab_maintainer', None)
                    if maintainer is not None:
                        maintainer.verify_access()
        run_background(check_account, ready)

    def setup_cruncher_tab(self):
        self._processing_busy = False
        self._processing_cancelling = False
        self._queue_undo = None
        layout = QVBoxLayout()
        layout.setSpacing(8)
        title_label = QLabel("Process the starting queue unattended. New recordings wait for the next run.")
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        video_actions = QHBoxLayout()
        self.btn_process_auto = QPushButton("Detect Cells…")
        set_button_color(self.btn_process_auto, ACTION_GREEN)
        self.btn_process_auto.clicked.connect(self.run_auto_cell_detection)
        video_actions.addWidget(self.btn_process_auto)

        self.btn_process_now = QPushButton("Select a Cell Manually…")
        self.btn_process_now.clicked.connect(self.select_and_process_file)
        video_actions.addWidget(self.btn_process_now)
        video_actions.addStretch()
        self.testing_controls = QWidget()
        testing_layout = QVBoxLayout(self.testing_controls)
        testing_layout.setContentsMargins(0, 0, 0, 0)
        testing_layout.addWidget(QLabel('Check processing settings on one file.'))
        testing_layout.addLayout(video_actions)
        self.testing_controls.hide()

        self.btn_process_batch = QPushButton("Process Queue")
        set_button_color(self.btn_process_batch, '#2D9CDB')
        self.btn_process_batch.setProperty('primary', True)
        self.btn_process_batch.clicked.connect(self.processing_action)

        self.chk_debug_avis = QCheckBox("Save Intermediate Cell Videos")
        self.chk_debug_avis.setChecked(False)
        self.chk_debug_avis.setToolTip("Save cropped and masked cell videos for imported recordings. Captures use their saved retention choices.")

        self.console_output = QTextEdit()
        self.console_output.setReadOnly(True)
        self.console_output.setTabChangesFocus(True)
        self.job_status = QLabel('No active processing job.')
        self.job_status.setWordWrap(True)
        layout.addWidget(self.job_status)
        self.queue_summary = QLabel('Queued videos')
        layout.addWidget(self.queue_summary)
        self.queue_error = ErrorDetails()
        layout.addWidget(self.queue_error)
        self.queue_list = QListWidget(); self.queue_list.setMinimumHeight(90)
        self.queue_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.queue_list, 1)
        self.queue_help = QLabel('Select an entry to locate a moved recording or remove it from the queue. Recording files are kept.')
        self.queue_help.setWordWrap(True); layout.addWidget(self.queue_help)
        self.queue_feedback = InlineNotice(); self.queue_feedback.setWordWrap(True)
        refresh_queue = QPushButton('Refresh Queue'); refresh_queue.clicked.connect(self.refresh_processing_queue)
        queue_actions = FlowRow()
        queue_actions.flow.addWidget(refresh_queue)
        self.queue_buttons = {}
        for label, action in [('Add Recordings', self.add_recordings), ('Locate Recording…', self.relink_recording), ('Remove from Queue', self.remove_recording), ('Undo Location Change', self.undo_location_change)]:
            button = QPushButton(label); button.clicked.connect(action)
            if label != 'Undo Location Change': queue_actions.flow.addWidget(button)
            self.queue_buttons[label] = button
        # One action changes with the job; cancellation uses the same cooperative path.
        self.btn_cancel_processing = self.btn_process_batch
        processing_action_row = QHBoxLayout()
        processing_action_row.addWidget(self.btn_process_batch)
        processing_action_row.addWidget(self.chk_debug_avis)
        processing_action_row.addStretch()
        layout.insertLayout(1, processing_action_row)
        self.queue_list.currentItemChanged.connect(self.update_queue_actions)
        layout.addWidget(queue_actions)
        feedback_row = QHBoxLayout()
        feedback_row.addWidget(self.queue_feedback,1)
        feedback_row.addWidget(self.queue_buttons['Undo Location Change'],0,Qt.AlignmentFlag.AlignRight)
        layout.addLayout(feedback_row)
        self.refresh_processing_queue()
        details_toggle = QPushButton('Show Processing Details'); details_toggle.setCheckable(True)
        details_toggle.toggled.connect(self.console_output.setVisible)
        details_toggle.toggled.connect(lambda visible: details_toggle.setText('Hide Processing Details' if visible else 'Show Processing Details'))
        layout.addWidget(details_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.console_output.setMaximumHeight(110); self.console_output.hide()
        layout.addWidget(self.console_output)
        self.processing_results = QListWidget()
        self.processing_results.setMinimumHeight(70)
        self.processing_results.setMaximumHeight(70)
        self.results_heading = QLabel('Recording outcomes')
        self.results_heading.hide()
        self.processing_results.hide()
        layout.addWidget(self.results_heading)
        layout.addWidget(self.processing_results)
        results_actions = FlowLayout()
        self.result_buttons = {}
        for label, action in [('Open Output Folder', self.open_processing_output), ('Open in Analysis', self.open_processing_analysis),
                              ('Set Frame Rate…',self.set_recording_frame_rate),('Reanalyze Traces…',self.reanalyze_saved_traces),
                              ('Correct Outlines…',self.correct_saved_outlines),
                              ('Retry Statistics', self.retry_statistics)]:
            button = QPushButton(label); button.clicked.connect(action); results_actions.addWidget(button)
            button.hide()
            self.result_buttons[label] = button
        layout.addLayout(results_actions)
        self.processing_results.currentItemChanged.connect(self.update_result_actions)
        self.update_result_actions()
        testing_toggle = self.testing_toggle = QPushButton('Single Recording ▾')
        testing_toggle.setCheckable(True)
        testing_toggle.toggled.connect(self.testing_controls.setVisible)
        testing_toggle.toggled.connect(lambda visible: testing_toggle.setText('Single Recording ▴' if visible else 'Single Recording ▾'))
        layout.addWidget(testing_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.testing_controls)
        try:
            tempo_backend.initialize_processing_storage()
            self.refresh_processing_queue()
            for result in tempo_backend.read_result_history(tempo_backend.QUEUE_FILE + '.results.json'):
                self.record_processing_result(result, persist=False)
        except (OSError, ValueError, TypeError) as exc:
            self.log_to_console(str(exc))
            self.queue_error.set_error('Processing history could not be loaded. Existing recordings and history were preserved.',exc)

        layout.addStretch(0)
        self.processing_content = QWidget()
        self.processing_content.setLayout(layout)
        self.processing_content.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.processing_scroll = QScrollArea()
        self.processing_scroll.setWidgetResizable(True)
        self.processing_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.processing_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.processing_scroll.setWidget(self.processing_content)
        outer = QVBoxLayout(self.tab_cruncher)
        outer.setContentsMargins(0,0,0,0)
        outer.addWidget(self.processing_scroll)

    def run_auto_cell_detection(self):
        """Milestone 1: Automated cell detection on reference frame 0 with visual review."""
        if self._processing_busy:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Raw Video for Automated Cell Extraction", 
            tempo_backend.BASE_DIR,  
            "Recordings (*.tif *.tiff *.mp4 *.avi *.mov *.MP4 *.AVI);;All Files (*)",
        )
        if not file_path:
            return

        dialog = AutoCellReviewDialog(file_path, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            cells_data = dialog.get_cells_data()
            if not cells_data:
                self.job_status.setText('No cells selected. Processing was not started.')
                self.log_to_console("No cells selected. Processing was not started.")
                return

            export_avis = self.chk_debug_avis.isChecked()
            mode_str = "saving intermediate cell videos" if export_avis else "without intermediate cell videos"
            self.log_to_console(f"Processing for {len(cells_data)} confirmed cells ({mode_str})...")

            self.lock_buttons()
            self.worker = ProcessingWorker(
                task_type="single", 
                filepath=file_path, 
                cells_data=cells_data, 
                export_debug_avis=export_avis
            )
            self.worker.state_signal.connect(self.job_status.setText)
            self.worker.log_signal.connect(self.log_to_console)
            self.worker.finished.connect(self.unlock_buttons)
            self.worker.result_signal.connect(self.record_processing_result)
            self.worker.start()
        else:
            self.job_status.setText('Cell review cancelled. Processing was not started.')
            self.log_to_console("Automated cell review cancelled.")

    def select_and_process_file(self):
        if self._processing_busy:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Raw Video", 
            tempo_backend.BASE_DIR,  
            "Recordings (*.tif *.tiff *.mp4 *.avi *.mov *.MP4 *.AVI);;All Files (*)",
        )
        
        if file_path:
            # Open legacy Native PyQt Dialog
            dialog = ROIDialog(file_path, self)
            
            if dialog.exec() == QDialog.DialogCode.Accepted:
                cells_data = dialog.get_cells_data()
                
                if not cells_data:
                    self.job_status.setText('Select a nonempty cell region before processing.')
                    self.log_to_console("Select a nonempty cell region before processing.")
                    return

                export_avis = self.chk_debug_avis.isChecked()
                self.lock_buttons()
                self.worker = ProcessingWorker(
                    task_type="single", 
                    filepath=file_path, 
                    cells_data=cells_data, 
                    export_debug_avis=export_avis
                )
                self.worker.state_signal.connect(self.job_status.setText)
                self.worker.log_signal.connect(self.log_to_console)
                self.worker.finished.connect(self.unlock_buttons)
                self.worker.result_signal.connect(self.record_processing_result)
                self.worker.start()
            else:
                self.job_status.setText('Cell selection cancelled. Processing was not started.')
                self.log_to_console("Cell selection cancelled. Processing was not started.")

    def refresh_processing_queue(self):
        previous = self.queue_list.currentItem()
        selected_path = previous.data(Qt.ItemDataRole.UserRole) if previous else None
        self.queue_list.clear()
        self.queue_error.clear()
        try:
            queue = tempo_backend.read_queue(tempo_backend.QUEUE_FILE)
            ready = 0
            for filename in queue:
                available = tempo_backend.readable_recording(filename)
                running = tempo_backend.queue_entry_running(filename)
                ready += int(available and not running)
                suffix = ' — processing elsewhere' if running else ('' if available else ' — missing or unreadable')
                item = QListWidgetItem(os.path.basename(filename) + suffix)
                item.setToolTip(filename); item.setData(Qt.ItemDataRole.UserRole, filename)
                self.queue_list.addItem(item)
                if filename == selected_path:
                    self.queue_list.setCurrentItem(item)
            self.queue_summary.setText(f'Queued videos ({len(queue)}); {ready} ready' if queue else 'No videos queued.')
            self.btn_process_batch.setEnabled(not getattr(self, '_processing_cancelling', False) if self._processing_busy else bool(ready))
            row_height = self.queue_list.fontMetrics().height() + 12
            self.queue_list.setMaximumHeight(max(90, row_height * max(1, len(queue)) + 12))
            if hasattr(self, 'queue_buttons'):
                self.update_queue_actions()
        except (OSError, ValueError, TypeError) as exc:
            self.queue_error.set_error('Could not read the processing queue. Check access to the queue file, then choose Refresh Queue.', exc)
            self.queue_summary.setText('Queue unavailable.')
            self.btn_process_batch.setEnabled(self._processing_busy and not getattr(self, '_processing_cancelling', False))

    def processing_action(self):
        if self._processing_busy:
            self.cancel_processing()
        else:
            self.run_batch_processing()

    def run_batch_processing(self):
        if self._processing_busy:
            return
        self.refresh_processing_queue()
        if not self.btn_process_batch.isEnabled(): return
        try:
            starting_queue = tempo_backend.read_queue(tempo_backend.QUEUE_FILE)
        except (OSError, ValueError) as exc:
            self.queue_error.set_error('Could not start processing. Refresh the queue before retrying.', exc)
            return
        self.lock_buttons()
        self.worker = ProcessingWorker(task_type="batch", export_debug_avis=self.chk_debug_avis.isChecked(),
                                       queue=starting_queue)
        self.worker.state_signal.connect(self.job_status.setText)
        self.worker.log_signal.connect(self.log_to_console)
        self.worker.finished.connect(self.unlock_buttons)
        self.worker.result_signal.connect(self.record_processing_result)
        self.worker.start()

    def lock_buttons(self):
        self._processing_busy = True
        self._processing_cancelling = False
        self._queue_undo = None
        self.btn_process_batch.setText('Cancel Processing')
        self.btn_process_batch.setEnabled(True)
        self.btn_process_auto.setEnabled(False)
        self.btn_process_now.setEnabled(False)
        self.update_result_actions()
        self.update_queue_actions()

    def log_to_console(self, message):
        from PyQt6.QtGui import QTextCursor
        cursor = self.console_output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(str(message).rstrip('\n') + '\n')
        self.console_output.setTextCursor(cursor)
        self.console_output.ensureCursorVisible()

    def unlock_buttons(self):
        worker = getattr(self, 'worker', None)
        if worker is not None and not worker.wait(0):
            QTimer.singleShot(50, self.unlock_buttons)
            return
        self._processing_busy = False
        self._processing_cancelling = False
        self.btn_process_batch.setText('Process Queue')
        self.btn_process_auto.setEnabled(True)
        self.btn_process_now.setEnabled(True)
        self.refresh_processing_queue()
        self.update_result_actions()
        if getattr(self, '_quit_after_processing', False):
            self._quit_after_processing = False
            QTimer.singleShot(0, self._retry_close)

    def cancel_processing(self):
        if getattr(self, 'worker', None) and self.worker.isRunning():
            self.worker.cancel()
            self._processing_cancelling = True
            self.btn_process_batch.setText('Cancelling…')
            self.btn_process_batch.setEnabled(False)

    def record_processing_result(self, result, persist=True):
        if persist and getattr(getattr(self, 'worker', None), 'task_type', None) != 'batch':
            try:
                tempo_backend.record_result(tempo_backend.QUEUE_FILE + '.results.json', result)
                if result.resolved:
                    tempo_backend.finalize_processing(result)
            except (OSError, ValueError, TypeError) as exc:
                self.log_to_console(f'Could not save processing history: {exc}')
                self.queue_error.set_error('Results were kept, but history or video retention could not finish. Temporary footage was preserved when available.',exc)
        for index in range(self.processing_results.count() - 1, -1, -1):
            previous = self.processing_results.item(index).data(Qt.ItemDataRole.UserRole)
            if previous.input_path == result.input_path and previous.output_dir == result.output_dir:
                self.processing_results.takeItem(index)
        item = QListWidgetItem(f'{os.path.basename(result.input_path)} — {result.outcome.replace("_", " ")}')
        item.setData(Qt.ItemDataRole.UserRole, result)
        item.setToolTip(result.error or result.output_dir)
        self.processing_results.addItem(item)
        self.results_heading.show()
        self.processing_results.show()
        row_height = self.processing_results.fontMetrics().height() + 12
        self.processing_results.setMaximumHeight(max(70, min(250, row_height * self.processing_results.count() + 12)))
        self.processing_results.setCurrentItem(item)

    def start_captured_processing(self, paths=None):
        pending = getattr(self,'_auto_capture_pending',set())
        if paths:
            pending.update(map(str,paths))
        self._auto_capture_pending = pending
        if getattr(self,'_processing_busy',False):
            QTimer.singleShot(500,self.start_captured_processing)
            return
        if not pending:
            return
        queue = sorted(pending)
        pending.clear()
        self.lock_buttons()
        self.worker = ProcessingWorker('batch',queue=queue)
        self.worker.state_signal.connect(self.job_status.setText)
        self.worker.log_signal.connect(self.log_to_console)
        self.worker.result_signal.connect(self.record_processing_result)
        self.worker.finished.connect(self.unlock_buttons)
        self.worker.start()

    def update_result_actions(self, *_):
        result = self.selected_processing_result()
        self.result_buttons['Open Output Folder'].setEnabled(bool(result and result.output_dir and Path(result.output_dir).is_dir()))
        self.result_buttons['Open in Analysis'].setEnabled(bool(result and result.results_csv and Path(result.results_csv).is_file()))
        self.result_buttons['Retry Statistics'].setEnabled(bool(result and not self._processing_busy and
            result.outcome == 'failure' and 'kinematics' in result.completed_stages and 'statistics' not in result.completed_stages))
        self.result_buttons['Set Frame Rate…'].setEnabled(bool(result and not self._processing_busy and Path(result.input_path).is_file()
            and any(word in result.error.lower() for word in ('frame rate','timestamps','frame timing'))))
        self.result_buttons['Reanalyze Traces…'].setEnabled(bool(result and result.output_dir and not self._processing_busy
            and 'kinematics' in result.completed_stages and list(Path(result.output_dir).glob('*_raw_kinematics.csv'))))
        self.result_buttons['Correct Outlines…'].setEnabled(bool(result and result.output_dir and not self._processing_busy
            and Path(result.output_dir,'processing.json').is_file() and 'detection' in result.completed_stages))
        for button in self.result_buttons.values():
            button.setVisible(button.isEnabled())

    def selected_processing_result(self):
        item = self.processing_results.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def set_recording_frame_rate(self):
        result=self.selected_processing_result()
        if result is None or self._processing_busy:return
        from PyQt6.QtWidgets import QInputDialog
        from tempo_recording import set_declared_rate
        import getpass
        rate,accepted=QInputDialog.getDouble(self,'Declare recording frame rate',
            'Frame rate (fps). This assumption will remain attached to the results.',100.,.001,100000.,6)
        if not accepted:return
        try:
            run_task(self,'Saving recording timing…',lambda:set_declared_rate(result.input_path,rate,'local:'+getpass.getuser()))
            tempo_backend.add_to_queue(result.input_path)
            self.refresh_processing_queue()
        except Exception as exc:
            self.queue_error.set_error('The frame rate could not be saved. Original recording and results were preserved.',exc)

    def reanalyze_saved_traces(self):
        result=self.selected_processing_result()
        if result is None or self._processing_busy:return
        if not confirm_action(self,'Reanalyze Traces',
            'Create a new analysis using current kinetics settings? Source videos are not required. Image extraction and optical-flow measurements will not be recalculated.',
            'Reanalyze Traces'):return
        self.lock_buttons()
        self.worker=ProcessingWorker('traces',filepath=result.output_dir)
        self.worker.state_signal.connect(self.job_status.setText)
        self.worker.log_signal.connect(self.log_to_console)
        self.worker.result_signal.connect(self.record_processing_result)
        self.worker.finished.connect(self.unlock_buttons)
        self.worker.start()

    def correct_saved_outlines(self):
        result=self.selected_processing_result()
        if result is None or self._processing_busy:return
        try:
            from tempo_recording import captured_source
            footage=run_task(self,'Checking retained footage…',lambda:captured_source(result))
            manifest=json.loads(Path(result.output_dir,'processing.json').read_text())
            dialog=AutoCellReviewDialog(footage,self,cells_data=manifest['cells'])
            dialog.setWindowTitle('Correct Outlines · New Analysis')
            cells=manifest['cells']
            if cells:
                dialog.well_input.setText(str(cells[0].get('well_id',dialog.well_input.text())))
                dialog.fov_input.setText(str(cells[0].get('fov_id',dialog.fov_input.text())))
            if dialog.exec()!=QDialog.DialogCode.Accepted:return
            corrected=dialog.get_cells_data()
            if not corrected:return
            self.lock_buttons()
            self.worker=ProcessingWorker('geometry',cells_data=corrected,source_result=result,
                                         export_debug_avis=self.chk_debug_avis.isChecked())
            self.worker.state_signal.connect(self.job_status.setText)
            self.worker.log_signal.connect(self.log_to_console)
            self.worker.result_signal.connect(self.record_processing_result)
            self.worker.finished.connect(self.unlock_buttons)
            self.worker.start()
        except Exception as exc:
            self.queue_error.set_error('Outline reanalysis could not start. Previous results were preserved.',exc)
            self.processing_scroll.ensureWidgetVisible(self.queue_error)

    def open_processing_output(self):
        result = self.selected_processing_result()
        if result and result.output_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(result.output_dir))

    def open_processing_analysis(self):
        result = self.selected_processing_result()
        if result and result.results_csv and Path(result.results_csv).is_file():
            self.tab_analyzer.load_csv(result.results_csv)
            self.tabs.setCurrentWidget(self.tab_analyzer)
        else:
            self.job_status.setText('The selected recording has no compiled results to open.')

    def retry_statistics(self):
        result = self.selected_processing_result()
        if self._processing_busy or result is None:
            return
        if result.outcome != 'failure' or 'kinematics' not in result.completed_stages or 'statistics' in result.completed_stages:
            self.job_status.setText('Select a run whose extraction completed but statistics failed.')
            return
        try:
            manifest = json.loads(Path(result.output_dir, 'processing.json').read_text())
            original_cells = manifest['provenance']['cells']
            original_export = manifest['provenance']['intermediate_videos']
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.job_status.setText('The original processing record is unavailable. Start a new attempt.')
            self.log_to_console(str(exc))
            return
        self.lock_buttons()
        self.worker = ProcessingWorker('single', filepath=result.input_path,
            cells_data=original_cells, export_debug_avis=original_export, retry_output=result.output_dir)
        self.worker.state_signal.connect(self.job_status.setText)
        self.worker.log_signal.connect(self.log_to_console)
        self.worker.result_signal.connect(self.record_processing_result)
        self.worker.finished.connect(self.unlock_buttons)
        self.worker.start()

    def add_recordings(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Add Recordings', tempo_backend.RAW_DIR, 'Recordings (*.tif *.tiff *.avi *.mp4 *.mov);;All Files (*)')
        try:
            for path in paths:
                from tempo_recording import read_metadata,recover_capture
                info=read_metadata(path)
                capture_sidecar=Path(path+'.capture.json')
                capture=json.loads(capture_sidecar.read_text()) if capture_sidecar.is_file() else {}
                interrupted=(info.get('status')!='completed' or capture.get('status','completed')!='completed')
                if info.get('temporary_owned') and interrupted and not info.get('recovery_copy'):
                    if not confirm_action(self,'Recover Interrupted Capture',
                        'Copy the readable frames from '+Path(path).name+' into a new recording? '
                        'The original will be kept. Recovered measurements will carry a partial-recording warning.',
                        'Recover Readable Frames'):
                        continue
                    destination=Path(tempo_backend.RAW_DIR)/'.capture-cache'/'recovered'
                    destination.mkdir(parents=True,exist_ok=True)
                    destination=destination/(Path(path).stem+'_recovered_'+str(time.time_ns())+'.tif')
                    path=run_task(self,'Recovering readable frames…',lambda source=path,target=destination:recover_capture(source,target))
                tempo_backend.add_to_queue(path)
        except (OSError, ValueError) as exc:
            self.queue_error.set_error('Could not add recordings. Existing queue preserved.', exc)
            return
        self.refresh_processing_queue()

    def update_queue_actions(self, *_):
        item = self.queue_list.currentItem()
        path = item.data(Qt.ItemDataRole.UserRole) if item else None
        editable = bool(path and not self._processing_busy and not tempo_backend.queue_entry_running(path))
        self.queue_buttons['Locate Recording…'].setEnabled(editable)
        self.queue_buttons['Locate Recording…'].setToolTip('Choose the recording’s new file location.')
        self.queue_buttons['Remove from Queue'].setEnabled(editable)
        undo = getattr(self, '_queue_undo', None)
        undo_available = False
        if undo and not self._processing_busy:
            old, new = undo
            try:
                queue = tempo_backend.read_queue(tempo_backend.QUEUE_FILE)
                undo_available = new in queue and old not in queue and not any(tempo_backend.queue_entry_running(p) for p in undo)
            except (OSError, ValueError):
                pass
        self.queue_buttons['Undo Location Change'].setEnabled(undo_available)
        self.queue_buttons['Undo Location Change'].setVisible(undo_available)
        if path:
            self.queue_help.setText('Choose the recording’s new file location. Selected: ' + path)
        else:
            self.queue_help.setText('Select an entry to locate a moved recording or remove it from the queue. Recording files are kept.')

    def relink_recording(self):
        item = self.queue_list.currentItem()
        if not item:
            self.queue_feedback.setText('Select a queue entry first. No location was changed.')
            return
        old = item.data(Qt.ItemDataRole.UserRole)
        if self._processing_busy or tempo_backend.queue_entry_running(old):
            self.queue_feedback.setText('This entry is processing. Its location was not changed.')
            self.update_queue_actions(); return
        path, _ = QFileDialog.getOpenFileName(self, 'Locate Recording — Choose Its New File Location', tempo_backend.RAW_DIR, 'Recordings (*.tif *.tiff *.avi *.mp4 *.mov);;All Files (*)')
        if not path or os.path.abspath(path) == old:
            self.queue_feedback.setText('No location was changed.')
            return
        if not confirm_action(self, 'Update Recording Location', f'Current location:\n{old}\n\nNew location:\n{os.path.abspath(path)}\n\nChoose the same recording at its new location. No files will be moved or deleted.', 'Update Location'):
            self.queue_feedback.setText('No location was changed.'); return
        try:
            tempo_backend.relink_queue(old, path)
        except (OSError, ValueError) as exc:
            self.queue_error.set_error('Could not update the recording location. Queue preserved.', exc); return
        self._queue_undo = (old, os.path.abspath(path))
        self.refresh_processing_queue()
        self.queue_feedback.setText('Recording location updated. Undo Location Change is available until the entry changes or starts processing.')

    def undo_location_change(self):
        undo = getattr(self, '_queue_undo', None)
        if not undo or self._processing_busy:
            return
        try:
            tempo_backend.undo_queue_location(*undo)
        except (OSError, ValueError) as exc:
            self.queue_error.set_error('Could not undo the location change. Queue preserved.', exc)
            self.update_queue_actions(); return
        self._queue_undo = None
        self.refresh_processing_queue()
        self.queue_feedback.setText('Original recording location restored. No recording files were changed.')

    def remove_recording(self):
        item = self.queue_list.currentItem()
        if not item:
            self.queue_feedback.setText('Select a queue entry first. Nothing was removed.'); return
        path = item.data(Qt.ItemDataRole.UserRole)
        if self._processing_busy or tempo_backend.queue_entry_running(path):
            self.queue_feedback.setText('This entry is processing. Nothing was removed.'); return
        if confirm_action(self, 'Remove from Queue', 'Remove this entry from pending work? The recording file will be kept.', 'Remove Entry'):
            try:
                tempo_backend.remove_from_queue([path])
            except (OSError, ValueError) as exc:
                self.queue_error.set_error('Could not update queue.', exc); return
            self.refresh_processing_queue()
            self.queue_feedback.setText('Entry removed from the queue. The recording file was kept.')

    def _retry_close(self):
        if not getattr(self, '_cancel_close_retry', False):
            self.close()

    def _resume_after_close_action(self):
        self._quit_annotation_confirmation = None
        self.tab_annotator._shutdown_requested = False
        self.tab_annotator.lease_timer.start()
        if any(record.get('sync_pending') or record.get('nuclear_sync_pending')
               for record in self.tab_annotator.universal_annotations.values()):
            self.tab_annotator._sync_timer.start(1000)
        self.tab_maintainer.timer.start()

    def request_close_action(self, action):
        """Run an exit action only after the normal shutdown guards settle."""
        self._close_action = action
        self._cancel_close_retry = False
        return self.close()

    def cancel_close_action(self):
        if getattr(self, '_close_action', None) is not None:
            self._close_action = None
            self._cancel_close_retry = True
            self._quit_after_processing = False
            self._resume_after_close_action()

    def capture_cleanup_finished(self, success):
        if success:
            QTimer.singleShot(0, self._retry_close)
        else:
            self.cancel_close_action()
            self._resume_after_close_action()
            self.statusBar().showMessage('Camera shutdown failed. See Acquisition for details.')
            self.tabs.setCurrentWidget(self.tab_capture)

    def closeEvent(self, event):
        self._cancel_close_retry = False
        tasks = pending_task_titles()
        if tasks:
            self.statusBar().showMessage('Waiting for the current operation to finish: ' + tasks[0])
            event.ignore(); return
        if getattr(self.tab_capture, '_camera_setup_busy', False):
            self.statusBar().showMessage('Finish or cancel camera setup before quitting.')
            event.ignore(); return
        worker = getattr(self, 'worker', None)
        if worker is not None and worker.isRunning():
            event.ignore()
            if not getattr(self, '_quit_after_processing', False):
                if confirm_action(self, 'Processing is running',
                    'Keep processing with this window open, or cancel processing and quit after the current operation settles. Partial outputs will be retained.',
                    'Cancel Processing and Quit'):
                    self._quit_after_processing = True
                    self.cancel_processing()
                else:
                    self.cancel_close_action()
            self.tabs.setCurrentWidget(self.tab_cruncher)
            return
        annotation_worker = getattr(self.tab_annotator, 'auto_worker', None)
        if annotation_worker is not None and not annotation_worker.wait(0):
            self.statusBar().showMessage('Cell detection is still running. Quit again after it finishes; your current draft is retained.')
            self.tabs.setCurrentWidget(self.tab_annotator)
            event.ignore(); return
        if (getattr(self.tab_annotator, '_sync_busy', False)
                or getattr(self.tab_annotator, '_nuclear_busy', False)):
            self.statusBar().showMessage('Waiting for the annotation sync outcome. Local drafts are retained; quit again when it finishes.')
            self.tabs.setCurrentWidget(self.tab_annotator)
            event.ignore(); return
        if self.tab_maintainer.pending_work():
            self.statusBar().showMessage('Waiting for the maintainer action to finish. Quit again after its outcome is confirmed.')
            self.tabs.setCurrentWidget(self.tab_maintainer)
            event.ignore()
            return
        review = getattr(self.tab_maintainer, 'review', None)
        if review is not None and not review.can_leave():
            self.statusBar().showMessage('Finish the pending review action or resolve unsaved review edits before quitting.')
            self.tabs.setCurrentWidget(self.tab_maintainer)
            event.ignore(); return
        save_uncertain = (getattr(self.tab_annotator, '_annotation_save_unconfirmed', False)
                          or not self.tab_annotator.btn_retry_import_save.isHidden())
        pending_records={name:record for name,record in self.tab_annotator.universal_annotations.items()
                         if record.get('sync_pending') or record.get('nuclear_sync_pending')}
        pending = not save_uncertain and bool(pending_records)
        confirmation=None
        if save_uncertain or pending:
            import hashlib
            state=dict(uncertain=bool(save_uncertain),pending=pending_records,
                       local=getattr(self.tab_annotator,'annotations',{}) if save_uncertain else {})
            confirmation=hashlib.sha256(json.dumps(state,sort_keys=True,default=str).encode()).hexdigest()
        if save_uncertain and getattr(self,'_quit_annotation_confirmation',None)!=confirmation:
            if not confirm_action(self, 'Close with Unsaved Annotation Changes?',
                    'Some annotation or imported-image details could not be saved. Closing may lose those details. '
                    'Cancel to resolve the storage error before closing.', 'Close Anyway'):
                self.cancel_close_action()
                event.ignore(); return
            self._quit_annotation_confirmation=confirmation
        if pending and getattr(self,'_quit_annotation_confirmation',None)!=confirmation:
            if not confirm_action(self, 'Close with Unsynced Changes',
                    'Some annotations are saved on this computer but have not reached the shared dataset. '
                    'Close and keep the local copies, or cancel to allow syncing to finish.', 'Close and Keep Local Copies'):
                self.cancel_close_action()
                event.ignore(); return
            self._quit_annotation_confirmation=confirmation
        # Capture includes a persistent serial service. Request its stop before
        # the generic thread guard; otherwise collection can keep that guard busy.
        capture_tools = getattr(self.tab_capture, 'capture_controls', None)
        if capture_tools is not None and not capture_tools.shutdown():
            self.statusBar().showMessage('Stopping capture and stimulation before closing…')
            event.ignore()
            QTimer.singleShot(50, self._retry_close)
            return
        threads = self.findChildren(QThread) + [worker]
        if any(thread is not None and not thread.wait(0) for thread in threads):
            self.statusBar().showMessage('Waiting for a worker to finish releasing its resources…')
            event.ignore()
            QTimer.singleShot(50, self._retry_close)
            return
        self.tab_annotator._shutdown_requested = True
        self.tab_annotator._sync_timer.stop()
        self.tab_annotator.lease_timer.stop()
        self.tab_maintainer.timer.stop()
        if not self.tab_capture.cleanup():
            self.statusBar().showMessage('Waiting for acquisition and camera shutdown. The window will close when shutdown finishes.')
            event.ignore(); return
        # Completed threads may still be running native thread-local destructors.
        # Synchronize without ever blocking the GUI while those finish.
        capture_workers = [getattr(self.tab_capture, name, None) for name in ('acq_worker', 'scout_worker', '_cleanup_worker')]
        if any(thread is not None and not thread.wait(0) for thread in capture_workers):
            self.statusBar().showMessage('Waiting for acquisition resources to finish closing…')
            event.ignore(); QTimer.singleShot(50, self._retry_close); return
        action = getattr(self, '_close_action', None)
        if action is not None:
            self._close_action = None
            if not action():
                self._resume_after_close_action()
                event.ignore()
                return
        if hasattr(self, 'activity_icon'):
            self.activity_icon.shutdown()
        super().closeEvent(event)


def run_desktop_application(app):
    """Run startup inside the main event loop so application Quit works there."""
    from tempo_tasks import install_quit_guard, dispose_closed_views
    from update_wizard import SplashScreen, SoftwareUpdateWizard, ModelWeightsUpdateWizard
    app.setQuitOnLastWindowClosed(False)
    install_quit_guard(app)
    views = []
    startup_error = []

    def start():
        try:
            from tempo_startup_diagnostics import record as startup_stage, finish as finish_diagnostics
            startup_stage('splash.started')
            splash = SplashScreen()
            views.append(splash)
            splash.exec()
            startup_stage('splash.completed')
            if getattr(app, '_tempo_quit_requested', False):
                return
            startup_stage('dashboard.started')
            window = TempoDashboard()
            views.append(window)
            window.show()
            startup_stage('dashboard.shown')
            finish_diagnostics()
            app.setQuitOnLastWindowClosed(True)

            def show_startup_updates():
                if not window.isVisible():
                    return
                if getattr(splash, 'software_update_available', False):
                    SoftwareUpdateWizard(window, commit_details=splash.update_details).exec()
                if window.isVisible() and getattr(splash, 'model_update_available', False):
                    ModelWeightsUpdateWizard(window).exec()

            if getattr(splash, 'update_available', False):
                QTimer.singleShot(600, show_startup_updates)
        except Exception as exc:
            from tempo_startup_diagnostics import record as startup_stage
            startup_stage('startup.failed', error=exc)
            startup_error.append(exc)
            app.exit(1)

    QTimer.singleShot(0, start)
    exit_code = app.exec()
    dispose_closed_views(*views)
    if startup_error:
        raise startup_error[0]
    return exit_code


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()

    if platform.system() == "Windows":
        try:
            import ctypes
            myappid = 'tempo.throughput.platform.1.0'
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)
    from tempo_ui import install_ui
    install_ui(app)
    app.setStyle("Fusion")
    app.setApplicationName("TEMPO")
    app.setApplicationDisplayName("TEMPO")

    icon_path = application_icon_path()
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(str(icon_path)))

    from tempo_first_run import run_packaged_setup
    startup_stage('setup.check.started')
    setup_ready = run_packaged_setup(app)
    startup_stage('setup.check.completed' if setup_ready else 'setup.canceled')
    exit_code = run_desktop_application(app) if setup_ready else 0
    from tempo_tasks import dispose_application
    dispose_application(app)
    sys.exit(exit_code)
