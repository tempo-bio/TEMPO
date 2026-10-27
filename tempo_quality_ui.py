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
        self.resize(880,480)
        layout=QVBoxLayout(self)
        note=QLabel('Flagged recordings are included unless you exclude them. Quality flags remain attached.')
        note.setWordWrap(True);layout.addWidget(note)
        beta=QLabel('Focus assessment is in beta.');beta.setStyleSheet('color: #99a3b0;');layout.addWidget(beta)
        records=data.drop_duplicates('Recording_ID')
        self.table=QTableWidget(len(records),4)
        self.table.setHorizontalHeaderLabels(['Recording','Quality flags','Include','Reason'])
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0,self.fontMetrics().horizontalAdvance('Recording_000000000000')+16)
        self.table.setColumnWidth(1,self.fontMetrics().horizontalAdvance('Measurement validity warning')+16)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch)
        self.rows=[]
        history=dataset_decisions(csv_path)['recordings']
        for index,(_,row) in enumerate(records.iterrows()):
            recording=str(row['Recording_ID'])
            labels={'focus_unavailable':'Focus unavailable','focus':'Focus', 'focus_override':'Focus override',
                    'focus_timeout':'Focus response timeout','nominal_timing':'Nominal frame timing',
                    'user_declared_timing':'User-declared timing','intensity_conversion':'8-bit conversion',
                    'possible_viability_effect':'Possible cell-viability effect',
                    'possible_measurement_validity_effect':'Possible measurement-validity effect',
                    'saturation':'Saturation','inadequate_signal':'Inadequate signal'}
            raw=str(row.get('Quality_Flags','Unknown'))
            flags=', '.join(labels.get(flag,flag.replace('_',' ').capitalize()) for flag in raw.split(';'))
            if flags in ('nan',''):flags='None reported'
            for column,text in ((0,recording),(1,flags)):
                item=QTableWidgetItem(text);item.setToolTip(text)
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
            self.rows.append((recording,check,reason,included,reason.text()))
        self.table.setMinimumWidth(0);layout.addWidget(self.table,1)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height()+18)
        self.error=ErrorDetails();layout.addWidget(self.error)
        actions=QHBoxLayout();actions.addStretch()
        cancel=QPushButton('Cancel');cancel.clicked.connect(self.reject);actions.addWidget(cancel)
        self.apply_button=QPushButton('Apply');self.apply_button.clicked.connect(self.save);actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        QShortcut(QKeySequence.StandardKey.Close,self,activated=self.reject)

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
