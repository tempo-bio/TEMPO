"""Maintainer Model Evaluation: automatic assignments, queued Colab stages and review."""
import copy
import time
import webbrowser
from pathlib import Path

from PyQt6 import sip
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QPushButton,QLineEdit,
    QSpinBox,QDoubleSpinBox,QCheckBox,QComboBox,QTabWidget,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,
    QScrollArea,QGraphicsView,QGraphicsScene,QGridLayout,QFileDialog,QDialog,QTextBrowser)
from tempo_config import get_base_dir,tempo_preferences
from tempo_hub import tempo_hub
from tempo_tasks import run_background
from tempo_ui import InlineNotice,ErrorDetails,FlowRow,confirm_action
import tempo_evaluation as service
import tempo_model_adoption as adoption


class LinkedImageView(QGraphicsView):
    moved=pyqtSignal()
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self));self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.horizontalScrollBar().valueChanged.connect(lambda _:self.moved.emit())
        self.verticalScrollBar().valueChanged.connect(lambda _:self.moved.emit())
        self.setMinimumSize(100,100)

    def image(self,path):
        pixmap=QPixmap(path)
        if pixmap.isNull() or pixmap.width()*pixmap.height()>64*1024*1024:
            raise ValueError('The saved evaluation image cannot be displayed.')
        self.scene().clear();self.scene().addPixmap(pixmap);self.setSceneRect(self.scene().itemsBoundingRect())
        self.fit_image()

    def fit_image(self):
        if not self.sceneRect().isEmpty():self.fitInView(self.sceneRect(),Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self,event):
        self.scale(1.25 if event.angleDelta().y()>0 else .8,1.25 if event.angleDelta().y()>0 else .8)
        self.moved.emit();event.accept()


def table(headers):
    view=QTableWidget(0,len(headers));view.setHorizontalHeaderLabels(headers)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setWordWrap(False);view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    view.verticalHeader().hide();view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.horizontalHeader().setStretchLastSection(True)
    view.setMinimumHeight(120)
    return view


def fill(view,rows):
    view.setRowCount(len(rows))
    for row,values in enumerate(rows):
        for column,value in enumerate(values):
            text=str(value);item=QTableWidgetItem(text);item.setToolTip(text);view.setItem(row,column,item)
    view.resizeRowsToContents()


def number(value):
    return 'Unavailable' if value is None else f'{value:.3f}'


def decision(check):
    if check.get('reason')=='Class absent from both arms':return 'Not applicable'
    if check.get('passed'):return 'Pass'
    if check.get('reason') or check.get('low') is None:return 'Inconclusive'
    return 'Does not pass'


class ModelEvaluationPage(QWidget):
    def __init__(self,maintainer):
        super().__init__(maintainer)
        self.maintainer=maintainer;self.payload=None;self._generation=0;self._busy=False;self._read_busy=False
        self._linked=False;self._allowed=False;self._context=None;self.protocol_descriptor=None;self.parameter_widgets={}
        outer=QVBoxLayout(self);selector=QHBoxLayout()
        self.evaluations=QComboBox();self.evaluations.setAccessibleName('Saved model evaluations')
        self.evaluations.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.evaluations.setMinimumContentsLength(12);self.evaluations.currentIndexChanged.connect(self.load_selected)
        selector.addWidget(self.evaluations,1)
        self.refresh=QPushButton('Refresh');self.refresh.clicked.connect(self.refresh_list);selector.addWidget(self.refresh)
        self.instructions_button=QPushButton('Instructions');self.instructions_button.clicked.connect(self.instructions);selector.addWidget(self.instructions_button)
        outer.addLayout(selector)
        self.notice=InlineNotice();outer.addWidget(self.notice);self.status=QLabel('Load an evaluation protocol to begin.');self.status.setWordWrap(True);outer.addWidget(self.status)
        self.error=ErrorDetails();outer.addWidget(self.error)
        actions=FlowRow()
        self.primary=QPushButton('Run Stage');self.primary.clicked.connect(self.primary_action)
        self.publish=QPushButton('Publish Models');self.publish.clicked.connect(self.publish_models)
        self.cancel=QPushButton('Cancel Run');self.cancel.clicked.connect(self.cancel_run)
        self.recover=QPushButton('Recover Stopped Run');self.recover.clicked.connect(self.recover_run)
        self.regular=QPushButton('Return to Regular Training');self.regular.clicked.connect(self.return_to_training)
        for button in (self.primary,self.publish,self.cancel,self.recover,self.regular):actions.flow.addWidget(button)
        outer.addWidget(actions)
        self.tabs=QTabWidget();outer.addWidget(self.tabs,1)
        def scroll(widget):
            panel=QScrollArea();panel.setWidgetResizable(True);panel.setWidget(widget);return panel
        setup=QWidget();setup_layout=QVBoxLayout(setup)
        self.form=QFormLayout();self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.name=QLineEdit();self.name.setPlaceholderText('Optional evaluation name');self.name.setMaxLength(100)
        self.protocol_path=QLineEdit();self.protocol_path.setReadOnly(True);self.protocol_path.setPlaceholderText('Select protocol.json')
        self.browse=QPushButton('Load Protocol');self.browse.clicked.connect(self.select_protocol)
        protocol_row=QWidget();row=QHBoxLayout(protocol_row);row.setContentsMargins(0,0,0,0);row.addWidget(self.protocol_path,1);row.addWidget(self.browse)
        self.gpu=QComboBox();self.gpu.addItems(['Any CUDA GPU','A100'])
        self.form.addRow('Name',self.name);self.form.addRow('Protocol',protocol_row);self.form.addRow('Required GPU',self.gpu)
        setup_layout.addLayout(self.form)
        self.settings_form=QFormLayout();self.settings_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow);setup_layout.addLayout(self.settings_form)
        buttons=FlowRow();self.create=QPushButton('Create Evaluation');self.create.clicked.connect(self.create_evaluation)
        self.save_notebook=QPushButton('Save Colab Notebook');self.save_notebook.clicked.connect(self.export_notebook)
        buttons.flow.addWidget(self.create);buttons.flow.addWidget(self.save_notebook);setup_layout.addWidget(buttons)
        self.summary=QLabel('');self.summary.setWordWrap(True);setup_layout.addWidget(self.summary)
        self.images=table(['Image','Role','Independent unit']);self.images.horizontalHeader().setStretchLastSection(False)
        self.images.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);setup_layout.addWidget(self.images,1)
        self.tabs.addTab(scroll(setup),'Images and Settings')
        result=QWidget();self.results_layout=QVBoxLayout(result)
        self.report_stage=QComboBox();self.report_stage.currentIndexChanged.connect(self.show_report)
        report_controls=QHBoxLayout();report_controls.addWidget(self.report_stage,1)
        self.run_details=QPushButton('Run Details');self.run_details.clicked.connect(self.show_run_details);report_controls.addWidget(self.run_details)
        self.results_layout.addLayout(report_controls)
        self.result_scope=QLabel('');self.result_scope.setWordWrap(True);self.results_layout.addWidget(self.result_scope)
        self.result_run_summary=QLabel('');self.result_run_summary.setWordWrap(True);self.results_layout.addWidget(self.result_run_summary)
        self.result_notice=InlineNotice();self.results_layout.addWidget(self.result_notice)
        self.report_tables=QWidget();self.report_tables_layout=QVBoxLayout(self.report_tables);self.report_tables_layout.setContentsMargins(0,0,0,0)
        self.results_layout.addWidget(self.report_tables);self.results_layout.addStretch()
        self.tabs.addTab(scroll(result),'Results')
        previews=QWidget();previews_layout=QVBoxLayout(previews)
        self.preview_image=QComboBox();self.preview_image.currentIndexChanged.connect(self.update_preview_roles);previews_layout.addWidget(self.preview_image)
        controls=QHBoxLayout();self.left_role=QComboBox();self.right_role=QComboBox()
        for combo in (self.left_role,self.right_role):combo.currentIndexChanged.connect(self.load_preview);controls.addWidget(combo,1)
        fit=QPushButton('Fit');fit.clicked.connect(self.fit_previews);controls.addWidget(fit);previews_layout.addLayout(controls)
        self.preview_notice=InlineNotice();previews_layout.addWidget(self.preview_notice)
        self.preview_panel=QWidget();self.preview_grid=QGridLayout(self.preview_panel);self.preview_grid.setContentsMargins(0,0,0,0)
        self.left=LinkedImageView();self.right=LinkedImageView()
        self.left.moved.connect(lambda:self.mirror(self.left,self.right));self.right.moved.connect(lambda:self.mirror(self.right,self.left))
        self.preview_grid.addWidget(self.left,0,0);self.preview_grid.addWidget(self.right,0,1);previews_layout.addWidget(self.preview_panel,1)
        self.tabs.addTab(scroll(previews),'Images')
        self.tabs.currentChanged.connect(lambda index:self.load_preview() if index==2 else None)
        self.timer=QTimer(self);self.timer.setInterval(15000);self.timer.timeout.connect(self.poll)
        self.show_payload(None)

    def select_protocol(self):
        path,_=QFileDialog.getOpenFileName(self,'Load Evaluation Protocol','','Evaluation protocol (protocol.json);;JSON files (*.json)')
        if path:self.load_protocol_file(path)

    def load_protocol_file(self,path):
        from tempo_evaluation_protocol import read_package
        try:
            descriptor,_=read_package(path)
            self.protocol_descriptor=descriptor;self.local_protocol_path=str(Path(path).resolve());self.protocol_path.setText(self.local_protocol_path)
            self.build_parameters(descriptor)
            self.status.setText(descriptor['name']);self.error.clear();self.update_actions()
        except Exception as error:self.error.set_error('The evaluation protocol could not be loaded.',error)

    def build_parameters(self,descriptor,values=None):
        while self.settings_form.count():
            item=self.settings_form.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        self.parameter_widgets={};values=values or {}
        for row in descriptor.get('parameters',[]):
            value=values.get(row['name'],row.get('default'));kind=row['type']
            if kind in ('integer','number'):
                widget=QSpinBox() if kind=='integer' else QDoubleSpinBox()
                widget.setRange(row.get('minimum',-2147483647),row.get('maximum',2147483647));widget.setValue(value)
            elif kind=='boolean':widget=QCheckBox();widget.setChecked(value)
            elif kind=='choice':
                widget=QComboBox()
                for choice in row['choices']:widget.addItem(str(choice),choice)
                widget.setCurrentIndex(widget.findData(value))
            else:widget=QLineEdit(value)
            self.settings_form.addRow(row['label'],widget);self.parameter_widgets[row['name']]=(kind,widget)
            signal=widget.valueChanged if kind in ('integer','number') else widget.toggled if kind=='boolean' else widget.currentIndexChanged if kind=='choice' else widget.textChanged
            signal.connect(self.update_parameter_visibility)
        self.update_parameter_visibility()

    def update_parameter_visibility(self,*_):
        from tempo_evaluation_protocol import matches,active_stages
        descriptor=self.payload['plan']['protocol'] if self.payload else self.protocol_descriptor
        if not descriptor:return
        values=self.parameters()
        for row in descriptor.get('parameters',[]):
            widget=self.parameter_widgets[row['name']][1];visible=matches(row.get('visible_when'),values)
            widget.setVisible(visible);label=self.settings_form.labelForField(widget)
            if label:label.setVisible(visible)
        self.align_form_labels()
        if not self.payload:
            try:self.summary.setText('Stages: '+' → '.join(row['label'] for row in active_stages(descriptor,values)))
            except ValueError as error:self.summary.setText(str(error))
            self.update_actions()

    def align_form_labels(self):
        labels=[]
        for form in (self.form,self.settings_form):
            for row in range(form.rowCount()):
                item=form.itemAt(row,QFormLayout.ItemRole.LabelRole)
                if item and item.widget() and not item.widget().isHidden():labels.append(item.widget())
        if labels:
            width=min(max(label.fontMetrics().horizontalAdvance(label.text()) for label in labels)+4,max(100,self.width()//3))
            for label in labels:label.setFixedWidth(width);label.setWordWrap(True)

    def parameters(self):
        return {name:widget.value() if kind in ('integer','number') else widget.isChecked() if kind=='boolean' else
                widget.currentData() if kind=='choice' else widget.text() for name,(kind,widget) in self.parameter_widgets.items()}

    def create_evaluation(self):
        path=self.protocol_path.text();values=self.parameters();gpu=self.gpu.currentText();name=self.name.text()
        def create():
            prepared=service.prepare_evaluation(tempo_hub,get_base_dir(),protocol_path=path,parameters=values,gpu=gpu,name=name)
            return service.save_evaluation(tempo_hub,*prepared)
        self.write(create,lambda plan:self.refresh_list(plan['id']))

    def primary_action(self):
        if not self.payload:return
        evaluation_id=self.payload['plan']['id'];phase=self.payload['state'].get('phase')
        if phase in ('awaiting_colab','running','preparing'):self.open_notebook();return
        stage=service.next_stage(self.payload)
        if stage:
            self.write(lambda:service.queue_evaluation(tempo_hub,evaluation_id,stage['id']),lambda _:self.load_selected())
        elif confirm_action(self,'Record evaluation decision?',
            'Record the passing decision and any proposed regular-training settings? Model weights will not be published or installed.', 'Record Decision'):
            self.write(lambda:service.approve_evaluation(tempo_hub,evaluation_id),lambda _:self.load_selected())

    def publish_models(self):
        if not self.payload:return
        evaluation_id=self.payload['plan']['id']
        def prepared(value):
            if not self.payload or self.payload['plan']['id']!=evaluation_id:return
            if confirm_action(self,'Publish evaluated models?',adoption.adoption_summary(value),'Publish Models'):
                self.write(lambda:adoption.publish_adoption(tempo_hub,value),lambda _:self.load_selected(),
                           notice='Publishing evaluated models…')
        self.write(lambda:adoption.prepare_adoption(tempo_hub,evaluation_id),prepared,
                   notice='Preparing evaluated models…')

    def show_payload(self,payload):
        self.payload=payload;self.report_stage.blockSignals(True);previous=self.report_stage.currentData();self.report_stage.clear()
        if payload:
            plan=payload['plan'];self.name.setText(plan['name']);self.gpu.setCurrentText(plan['gpu'])
            self.protocol_path.setText(plan['protocol']['name']+' · '+plan['protocol']['version']);self.build_parameters(plan['protocol'],plan['parameters'])
            fill(self.images,[(name,{'train':'Training','val':'Validation','test':'Evaluation'}[row['split']],row['unit']) for name,row in sorted(plan['manifest']['images'].items())])
            roles={'train':'Training','val':'Validation','test':'Evaluation'}
            self.summary.setText(' · '.join(f'{roles.get(role,role)}: {count}' for role,count in plan['partitions'].items()))
            state=payload['state'];phase=state.get('phase','ready')
            stage=service.next_stage(payload) if phase=='ready' else next((row for row in service.plan_stages(plan) if row['id']==state.get('stage')),None)
            label=stage['label'] if stage else ''
            self.status.setText({'ready':'Ready','awaiting_colab':'Awaiting Colab','preparing':'Preparing','running':'Running',
                'completed':'Evaluation complete','failed':'Stage failed','cancelled':'Cancelled','disconnected':'Disconnected'}.get(phase,phase)+((' · '+label) if label else ''))
            if phase=='running' and state.get('task'):self.status.setText(self.status.text()+' · '+str(state['task']))
            if state.get('error'):self.error.set_error('Evaluation needs attention.',state['error'])
            for row in service.plan_stages(plan):
                if payload['reports'].get(row['id']):self.report_stage.addItem(row['label'],row['id'])
            index=self.report_stage.findData(previous);self.report_stage.setCurrentIndex(index if index>=0 else self.report_stage.count()-1)
        else:
            fill(self.images,[]);self.summary.clear()
            self.name.clear()
            self.status.setText(self.protocol_descriptor['name'] if self.protocol_descriptor else 'Load an evaluation protocol to begin.')
            if self.protocol_descriptor:self.build_parameters(self.protocol_descriptor);self.protocol_path.setText(self.local_protocol_path)
            else:self.protocol_path.clear()
        self.report_stage.blockSignals(False);self.show_report();self.update_actions()

    def show_report(self,*_):
        while self.report_tables_layout.count():
            item=self.report_tables_layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
        stage=self.report_stage.currentData();report=self.payload['reports'].get(stage,{}) if self.payload else {}
        self.result_scope.setText('Scope: '+str(self.payload['plan']['manifest'].get('scope_label',self.payload['plan']['scope'])) if self.payload else '')
        self.run_details.setEnabled(bool(report));self.result_run_summary.clear()
        if report:
            plan=self.payload['plan'];execution=report.get('execution',{})
            elapsed=execution.get('elapsed_seconds');runtime=f'{elapsed:.1f} s' if elapsed is not None else 'Not reported'
            self.result_run_summary.setText(f"Images: {len(plan['manifest']['images'])} · Execution time: {runtime} · GPU: {execution.get('gpu','Not reported')}")
        self.result_notice.setText(report.get('summary','No results have been reported.'))
        self.result_tables=[]
        for item in report.get('tables',[]):
            label=QLabel(item.get('title',''));label.setWordWrap(True);self.report_tables_layout.addWidget(label)
            view=table(item['columns']);view.setWordWrap(True);view.setTextElideMode(Qt.TextElideMode.ElideNone)
            view.horizontalHeader().setStretchLastSection(False);view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            fill(view,[['Unavailable' if value is None else
                        (f'{value:.3g}' if 0<abs(value)<.0005 else f'{value:.3f}') if isinstance(value,float) else value
                        for value in row] for row in item['rows']])
            view.setMinimumHeight(max(150,view.fontMetrics().lineSpacing()*5+view.horizontalHeader().height()))
            self.report_tables_layout.addWidget(view);self.result_tables.append(view)
        self.preview_image.blockSignals(True);previous=self.preview_image.currentData();self.preview_image.clear()
        for row in report.get('previews',[]):self.preview_image.addItem(row['name'],row['name'])
        index=self.preview_image.findData(previous);self.preview_image.setCurrentIndex(index if index>=0 else 0);self.preview_image.blockSignals(False)
        self.update_preview_roles()

    def show_run_details(self):
        import json
        if not self.payload:return
        plan=self.payload['plan'];stage=self.report_stage.currentData();report=self.payload['reports'].get(stage,{})
        metadata=dict(evaluation_id=plan['id'],protocol=plan['protocol']['id'],protocol_version=plan['protocol']['version'],
            scope=plan['scope'],parameters=plan['parameters'],partitions=plan['partitions'],execution=report.get('execution',{}),
            dataset_revision=plan.get('dataset_revision'),model_revision=plan.get('baseline_revision'),
            engine_sha256=plan.get('bundle_sha256'),protocol_sha256=plan.get('protocol_sha256'),report_sha256=report.get('report_sha256'))
        dialog=QDialog(self);dialog.setWindowTitle('Evaluation Run Details');dialog.resize(720,540)
        layout=QVBoxLayout(dialog);reader=QTextBrowser();reader.setPlainText(json.dumps(metadata,indent=2));layout.addWidget(reader)
        close=QPushButton('Close');close.clicked.connect(dialog.accept);layout.addWidget(close,0,Qt.AlignmentFlag.AlignRight)
        dialog.exec();dialog.deleteLater()

    def update_preview_roles(self,*_):
        stage=self.report_stage.currentData();report=self.payload['reports'].get(stage,{}) if self.payload else {}
        preview=next((row for row in report.get('previews',[]) if row['name']==self.preview_image.currentData()),None)
        roles=list(preview['views']) if preview else []
        for index,combo in enumerate((self.left_role,self.right_role)):
            previous=combo.currentText();combo.blockSignals(True);combo.clear();combo.addItems(roles)
            combo.setCurrentText(previous if previous in roles else roles[min(index,len(roles)-1)] if roles else '')
            combo.blockSignals(False)
        if not preview:
            self.left.scene().clear();self.right.scene().clear();self.preview_notice.setText('No images to compare for this stage.')
        else:self.load_preview()

    def load_preview(self,*_):
        if self.tabs.currentIndex()!=2 or not self.payload or not self.preview_image.currentData():return
        name=self.preview_image.currentData();stage=self.report_stage.currentData();roles=[self.left_role.currentText(),self.right_role.currentText()]
        key=(self.payload['plan']['id'],stage,self.payload['reports'][stage].get('report_sha256'),name,tuple(roles))
        if key==getattr(self,'_preview_key',None):return
        payload=self.payload;self.preview_notice.setText('Loading saved images…')
        def ready(paths):
            if name!=self.preview_image.currentData() or stage!=self.report_stage.currentData() or roles!=[self.left_role.currentText(),self.right_role.currentText()]:return
            try:
                self._linked=True;self.left.image(paths[0]);self.right.image(paths[1]);self.preview_notice.clear();self._preview_key=key
            except Exception as error:self.error.set_error('The saved images could not be displayed.',error)
            finally:self._linked=False
        self.read(lambda:service.load_evidence(tempo_hub,payload,name,roles,stage),ready)

    def update_actions(self):
        payload=self.payload;phase=payload['state'].get('phase','ready') if payload else 'ready'
        active=payload and phase in ('awaiting_colab','preparing','running');stage=service.next_stage(payload) if payload else None
        complete=payload and stage is None
        decision=payload['reports'].get(service.plan_stages(payload['plan'])[-1]['id'],{}).get('decision',{}) if complete else {}
        publishable=bool(complete and payload['state'].get('adoption_recorded') and adoption.proposal_for(payload))
        published=bool(payload and payload.get('deployment',{}).get('model_revision'))
        self.publish.setVisible(publishable or published)
        self.publish.setText('Models Published' if published else 'Publish Models')
        self.publish.setEnabled(publishable and not published and not self._busy and not self._read_busy)
        self.name.setEnabled(not payload and not self._busy);self.gpu.setEnabled(not payload and not self._busy)
        self.browse.setEnabled(not payload and not self._busy)
        for _,widget in self.parameter_widgets.values():widget.setEnabled(not payload and not self._busy)
        valid=bool(self.protocol_descriptor and self.protocol_path.text())
        if valid and not payload:
            from tempo_evaluation_protocol import active_stages
            try:active_stages(self.protocol_descriptor,self.parameters())
            except ValueError:valid=False
        self.create.setEnabled(valid and not payload and not self._busy)
        self.create.setVisible(not payload);self.save_notebook.setEnabled(not self._busy)
        self.evaluations.setEnabled(not self._busy);self.refresh.setEnabled(self._allowed and not self._busy)
        self.cancel.setVisible(bool(active));self.cancel.setEnabled(bool(active) and not self._busy)
        self.regular.setVisible(bool(payload and payload.get('intent') and not active));self.regular.setEnabled(not self._busy)
        self.recover.setVisible(bool(payload and phase in ('running','preparing') and time.time()-payload['state'].get('updated_at',time.time())>120));self.recover.setEnabled(not self._busy)
        if active:text='Open Notebook'
        elif complete:text='Decision Recorded' if payload['state'].get('adoption_recorded') else 'Record Decision' if decision.get('passed') is True else 'Evaluation Complete'
        elif stage:text='Resume Stage' if phase in ('failed','cancelled','disconnected') else 'Run Stage'
        else:text='Run Stage'
        self.primary.setText(text);self.primary.setVisible(bool(payload))
        enabled=bool(payload) and not self._busy
        if active:enabled=enabled and payload.get('intent',{}).get('evaluation_id')==payload['plan']['id']
        if complete:enabled=enabled and decision.get('passed') is True and not payload['state'].get('adoption_recorded')
        self.primary.setEnabled(bool(enabled))

    def poll(self):
        if self.isVisible() and self._allowed and self.payload and not self._busy and not self._read_busy:
            self.load_selected()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not hasattr(self,'preview_grid'):return
        self.align_form_labels()
        stacked=self.width()<850 or self.fontMetrics().height()>26
        self.preview_grid.addWidget(self.right,1 if stacked else 0,0 if stacked else 1)
        for view in getattr(self,'result_tables',[]):view.resizeRowsToContents()
    def context(self):
        return tempo_hub.token,tempo_hub.get_dataset_repo(),tempo_hub.get_models_repo()

    def set_allowed(self,allowed):
        context=self.context()
        if context!=self._context or not allowed:
            self._generation+=1;self.payload=None;self._read_busy=False
            self.evaluations.blockSignals(True);self.evaluations.clear();self.evaluations.blockSignals(False)
            self.show_payload(None)
        self._context=context;self._allowed=allowed
        self.setEnabled(allowed)
        if allowed:self.timer.start()
        else:self.timer.stop()

    def read(self,action,completed):
        if self._read_busy:return
        self._read_busy=True;generation=self._generation;context=self.context()
        deadline=QTimer(self);deadline.setSingleShot(True)
        def expired():
            if generation==self._generation and context==self.context():
                self._generation+=1;self._read_busy=False
                self.error.set_error('Model Evaluation took too long to load. Refresh to retry.',TimeoutError('No response within 30 seconds.'))
        deadline.timeout.connect(expired);deadline.start(30000)
        def ready(value,error):
            if sip.isdeleted(self):return
            deadline.stop();deadline.deleteLater()
            if context!=self.context() or generation!=self._generation:return
            self._read_busy=False
            if error:self.error.set_error('Model Evaluation could not be loaded. Refresh to retry.',error,secrets=(tempo_hub.token,))
            else:
                self.error.clear();completed(value)
        run_background(action,ready)

    def write(self,action,completed,*,notice='Saving evaluation changes…'):
        if self._busy:return
        self._busy=True;context=self.context();self.error.clear();self.notice.setText(notice);self.update_actions()
        def ready(value,error):
            if sip.isdeleted(self):return
            self._busy=False
            if context!=self.context():return
            self.notice.clear()
            if error:self.error.set_error('Model Evaluation did not finish this action. Review the details before retrying.',error,secrets=(tempo_hub.token,))
            else:completed(value)
            self.update_actions()
        self.maintainer.run_write(action,ready)

    def refresh_list(self,selected=None):
        if not self._allowed:return
        selected=selected or self.evaluations.currentData()
        def ready(entries):
            self.evaluations.blockSignals(True);self.evaluations.clear()
            self.evaluations.addItem('New evaluation',None)
            for entry in entries:self.evaluations.addItem(entry['name'],entry['id'])
            index=self.evaluations.findData(selected)
            if index>=0:self.evaluations.setCurrentIndex(index)
            self.evaluations.blockSignals(False);self.load_selected()
        self.read(lambda:service.list_evaluations(tempo_hub),ready)

    def load_selected(self,*_):
        evaluation_id=self.evaluations.currentData()
        if not evaluation_id:self.show_payload(None);return
        self._generation+=1;self._read_busy=False
        self.read(lambda:service.load_evaluation(tempo_hub,evaluation_id),self.show_payload)

    def open_notebook(self):
        from maintainer_tab import colab_url,COLAB_HOME
        try:
            value=tempo_preferences.get('collaboration','colab_notebook_url','')
            if not webbrowser.open(colab_url(value) if value else COLAB_HOME):raise OSError('The browser could not be opened.')
            self.notice.setText('In Colab, choose Run all.' if value else 'Upload the saved Colab notebook, connect its GPU runtime and choose Run all.')
        except Exception as error:self.error.set_error('The Colab notebook could not be opened.',error)

    def cancel_run(self):
        if self.payload:
            evaluation_id=self.payload['plan']['id']
            self.write(lambda:service.cancel_evaluation(tempo_hub,evaluation_id),lambda _:self.load_selected())

    def recover_run(self):
        if self.payload and confirm_action(self,'Recover stopped evaluation?',
            'Stop the previous Colab execution before recovering. Recovery does not stop that process.', 'Recover Run'):
            evaluation_id=self.payload['plan']['id']
            self.write(lambda:service.recover_evaluation(tempo_hub,evaluation_id),lambda _:self.load_selected())

    def return_to_training(self):
        def ready(_):
            self.load_selected()
            if hasattr(self.maintainer,'pages'):self.maintainer.pages.setCurrentWidget(self.maintainer.cloud_scroll)
        self.write(lambda:service.clear_evaluation_intent(tempo_hub),ready)

    def export_notebook(self):
        path,_=QFileDialog.getSaveFileName(self,'Save Colab Notebook','TEMPO_Colab_Trainer.ipynb','Colab notebook (*.ipynb)')
        if not path:return
        try:
            import json
            from notebooks.build_staged_colab_notebook import build_notebook
            Path(path).write_text(json.dumps(build_notebook(tempo_hub.get_dataset_repo(),models_repo=tempo_hub.get_models_repo()),indent=1)+'\n')
        except Exception as error:self.error.set_error('The Colab notebook could not be saved.',error)

    def instructions(self):
        from maintainer_tab import TrainingWorkflowDialog
        try:
            dialog=TrainingWorkflowDialog(self,source_name='MODEL_EVALUATION_WORKFLOW.md',title='Model Evaluation')
            dialog.exec();dialog.deleteLater()
        except OSError as error:self.error.set_error('Model Evaluation instructions could not be opened.',error)

    def mirror(self,source,target):
        if self._linked:return
        self._linked=True
        try:target.setTransform(source.transform());target.centerOn(source.mapToScene(source.viewport().rect().center()))
        finally:self._linked=False

    def fit_previews(self):
        self._linked=True
        try:self.left.fit_image();self.right.fit_image()
        finally:self._linked=False
