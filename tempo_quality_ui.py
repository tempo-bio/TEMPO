"""Recording-level quality decisions; assessments remain attached to the data."""
from pathlib import Path
import getpass
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence,QShortcut
from PyQt6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QTableWidget,
    QTableWidgetItem,QHeaderView,QCheckBox,QLineEdit,QPushButton)
from tempo_ui import ErrorDetails
from tempo_quality import dataset_decisions,decide_dataset
from tempo_recording import sha256


class InclusionCheckBox(QCheckBox):
    def hitButton(self,position):
        return self.rect().contains(position)


class QualityReviewDialog(QDialog):
    def __init__(self,csv_path,data,parent=None):
        super().__init__(parent)
        self.csv_path=str(csv_path)
        self.source_digest=sha256(csv_path)
        self.setWindowTitle('TEMPO · Recording quality')
        self.resize(920,480)
        layout=QVBoxLayout(self)
        note=QLabel('Flagged recordings are included unless you exclude them. Double-click any row or click Inspect to view that field on the map.')
        note.setWordWrap(True);layout.addWidget(note)
        beta=QLabel('Focus assessment is in beta.');beta.setStyleSheet('color: #99a3b0;');layout.addWidget(beta)
        records=data.drop_duplicates('Recording_ID')
        self.table=QTableWidget(len(records),5)
        self.table.setHorizontalHeaderLabels(['Well & Field','Quality flags & issues','Include','Reason','Action'])
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0,self.fontMetrics().horizontalAdvance('Well W99 · Field FOV999')+24)
        self.table.setColumnWidth(1,self.fontMetrics().horizontalAdvance('Measurement validity warning · Cell 999')+24)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.ResizeToContents)
        self.rows=[]
        self.row_targets=[]
        history=dataset_decisions(csv_path)['recordings']
        for index,(_,row) in enumerate(records.iterrows()):
            recording=str(row['Recording_ID'])
            well=str(row.get('Well_ID','Unknown'))
            fov=str(row.get('FOV_ID','Unknown'))
            fov_clean=fov.removeprefix('FOV').lstrip('0') or fov
            location_text=f"Well {well} · Field {fov_clean}" if well!='Unknown' else recording
            self.row_targets.append((well,fov,recording))
            labels={'focus_unavailable':'Focus unavailable','focus':'Focus', 'focus_override':'Focus override',
                    'focus_timeout':'Focus response timeout','nominal_timing':'Nominal frame timing',
                    'user_declared_timing':'User-declared timing','intensity_conversion':'8-bit conversion',
                    'possible_viability_effect':'Possible cell-viability effect',
                    'possible_measurement_validity_effect':'Possible measurement-validity effect',
                    'saturation':'Saturation','inadequate_signal':'Inadequate signal',
                    'low_amplitude':'Low amplitude','irregular_beat':'Irregular beat'}
            rec_cells=data[data['Recording_ID']==recording]
            flagged_cells=rec_cells[rec_cells['Quality_Flags'].fillna('').astype(str).ne('')]
            raw=str(row.get('Quality_Flags','Unknown'))
            flags_list=[labels.get(flag,flag.replace('_',' ').capitalize()) for flag in raw.split(';') if flag.strip() and flag!='nan']
            if flagged_cells.empty and not flags_list:
                flags_display='None reported'
            else:
                flags_desc=', '.join(flags_list) if flags_list else 'Flagged'
                if not flagged_cells.empty and len(flagged_cells)<len(rec_cells):
                    cids=', '.join(str(cid) for cid in flagged_cells['Cell_ID'].head(2))
                    if len(flagged_cells)>2: cids+=f' +{len(flagged_cells)-2}'
                    flags_display=f"{flags_desc} ({cids})"
                else:
                    flags_display=flags_desc
            for column,text in ((0,location_text),(1,flags_display)):
                item=QTableWidgetItem(text);item.setToolTip(f"Recording: {recording}\nWell: {well}\nField: {fov}")
                item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(index,column,item)
            check=InclusionCheckBox();check.setAccessibleName('Include '+recording)
            included=str(row.get('Quality_Included',True)).lower() in ('true','1')
            check.setChecked(included);self.table.setCellWidget(index,2,check)
            reason=QLineEdit();reason.setPlaceholderText('Optional');reason.setAccessibleName('Reason for '+recording)
            reason.setMaximumHeight(reason.sizeHint().height())
            decisions=history.get(recording,{}).get('decisions',[])
            if decisions:reason.setText(decisions[-1].get('reason',''))
            self.table.setCellWidget(index,3,reason)
            btn_inspect=QPushButton('Inspect')
            btn_inspect.setToolTip(f'View Well {well}, Field {fov_clean} on map')
            btn_inspect.clicked.connect(lambda _,w=well,f=fov: self.inspect_recording(w,f))
            self.table.setCellWidget(index,4,btn_inspect)
            self.rows.append((recording,check,reason,included,reason.text()))
        self.table.cellDoubleClicked.connect(self._on_double_clicked)
        self.table.setMinimumWidth(0);layout.addWidget(self.table,1)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height()+18)
        self.error=ErrorDetails();layout.addWidget(self.error)
        actions=QHBoxLayout();actions.addStretch()
        cancel=QPushButton('Cancel');cancel.clicked.connect(self.reject);actions.addWidget(cancel)
        self.apply_button=QPushButton('Apply');self.apply_button.clicked.connect(self.save);actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        QShortcut(QKeySequence.StandardKey.Close,self,activated=self.reject)

    def _on_double_clicked(self,row,_col):
        if 0<=row<len(self.row_targets):
            well,fov,_=self.row_targets[row]
            self.inspect_recording(well,fov)

    def inspect_recording(self,well,fov):
        parent=self.parent()
        if parent is not None:
            if hasattr(parent,'load_well') and well!='Unknown':
                parent.load_well(well)
            if hasattr(parent,'field_selector') and fov!='Unknown':
                for i in range(parent.field_selector.count()):
                    item_data=parent.field_selector.itemData(i)
                    if item_data is not None and len(item_data)==2 and item_data[1]==fov:
                        parent.field_selector.setCurrentIndex(i)
                        break
        self.accept()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'table'):
            self.table.resizeRowsToContents()

    def save(self):
        changes={recording:dict(included=check.isChecked(),reason=reason.text().strip())
                 for recording,check,reason,old,old_reason in self.rows
                 if check.isChecked()!=old or reason.text().strip()!=old_reason}
        try:
            if sha256(self.csv_path)!=self.source_digest:
                raise ValueError('The results file changed during review. Reopen Recording Quality before applying decisions.')
            if changes:decide_dataset(self.csv_path,changes,'local:'+getpass.getuser())
        except Exception as exc:
            self.error.set_error('Quality decisions could not be saved. Your selections remain available.',exc)
            return
        self.accept()
