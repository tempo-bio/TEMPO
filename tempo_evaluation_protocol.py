"""Versioned, developer-supplied evaluation packages and their execution context."""
from contextlib import contextmanager
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import sys
import uuid
import zipfile

FORMAT='tempo-evaluation-protocol-v1'
MAX_BYTES=20*1024*1024


def slug(value):
    if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',value):
        raise ValueError('Protocol and stage identifiers must be short lowercase names.')
    return value


def matches(condition,values):
    return all(values.get(name) in expected if isinstance(expected,list) else values.get(name)==expected
               for name,expected in (condition or {}).items())


def active_stages(descriptor,values):
    stages=[row for row in descriptor['stages'] if matches(row.get('when'),values)]
    if not stages:raise ValueError('These settings leave no active evaluation stages.')
    return stages


def relative_path(value):
    path=PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or path.as_posix()!=value:
        raise ValueError('Invalid protocol file path.')
    return value


def validate_descriptor(value):
    value=copy.deepcopy(value)
    if not isinstance(value,dict) or value.get('format')!=FORMAT:
        raise ValueError('Select a versioned TEMPO evaluation protocol.')
    slug(value['id'])
    if not isinstance(value.get('version'),str) or not value['version'].strip():raise ValueError('A protocol version is required.')
    if not isinstance(value.get('name'),str) or not value['name'].strip():raise ValueError('A protocol name is required.')
    stages=value.get('stages',[])
    if not stages or len(stages)>100:raise ValueError('A protocol needs between one and 100 stages.')
    ids=[]
    for stage in stages:
        ids.append(slug(stage['id']))
        if not isinstance(stage.get('label'),str) or not stage['label'].strip():raise ValueError('Each stage needs a label.')
    if len(ids)!=len(set(ids)):raise ValueError('Protocol stage identifiers must be unique.')
    names=[]
    for parameter in value.get('parameters',[]):
        names.append(slug(parameter['name']))
        if parameter.get('type') not in ('integer','number','text','boolean','choice'):raise ValueError('Unknown protocol setting type.')
        if not parameter.get('label'):raise ValueError('Each protocol setting needs a label.')
        if parameter['type']=='choice' and not parameter.get('choices'):raise ValueError('A choice setting needs options.')
    if len(names)!=len(set(names)) or len(names)>100:raise ValueError('Protocol settings must have unique names.')
    for row,key in [(row,'visible_when') for row in value.get('parameters',[])]+[(row,'when') for row in stages]:
        condition=row.get(key,{})
        if not isinstance(condition,dict) or set(condition)-set(names):raise ValueError('Conditional controls must reference declared settings.')
        if key=='visible_when' and row['name'] in condition:raise ValueError('A setting cannot hide itself.')
    files=value.get('files',[])
    if len(files)!=len(set(files)) or 'protocol.json' in files:raise ValueError('Protocol files must be unique.')
    for name in files:relative_path(name)
    entry=value.get('entrypoint','protocol.py')
    if entry not in files or not entry.endswith('.py'):raise ValueError('The protocol entry point must be a bundled Python file.')
    if value.get('requirements') is not None and value['requirements'] not in files:
        raise ValueError('Protocol requirements must be included in its file list.')
    return value


def parameter_values(descriptor, values=None):
    supplied=values or {};result={}
    known={row['name'] for row in descriptor.get('parameters',[])}
    if set(supplied)-known:raise ValueError('Unknown evaluation settings.')
    for row in descriptor.get('parameters',[]):
        value=supplied.get(row['name'],row.get('default'))
        kind=row['type']
        valid=(type(value) is int if kind=='integer' else type(value) in (int,float) if kind=='number' else
               type(value) is bool if kind=='boolean' else isinstance(value,str) if kind=='text' else value in row['choices'])
        if not valid:raise ValueError('Invalid setting: '+row['label'])
        if kind in ('integer','number') and (value<row.get('minimum',float('-inf')) or value>row.get('maximum',float('inf'))):
            raise ValueError('Setting outside its allowed range: '+row['label'])
        result[row['name']]=value
    json.dumps(result,allow_nan=False)
    return result


def read_package(path):
    path=Path(path);folder=path.parent
    descriptor=validate_descriptor(json.loads(path.read_text()))
    parameter_values(descriptor)
    stream=io.BytesIO();size=0
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        def put(name,data):
            entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.create_system=3;entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,data)
        put('protocol.json',json.dumps(descriptor,sort_keys=True))
        for name in descriptor['files']:
            source=folder/name
            if source.is_symlink() or not source.resolve().is_relative_to(folder.resolve()):raise ValueError('Protocol files must stay inside their folder.')
            data=source.read_bytes();size+=len(data)
            if size>MAX_BYTES:raise ValueError('Protocol sources exceed 20 MiB; store model weights as artifacts.')
            if name.endswith('.py'):compile(data,name,'exec')
            put(name,data)
    payload=stream.getvalue()
    if len(payload)>MAX_BYTES:raise ValueError('Protocol archive exceeds 20 MiB.')
    return descriptor,payload


def unpack_package(payload,folder,expected):
    if len(payload)>MAX_BYTES or hashlib.sha256(payload).hexdigest()!=expected:
        raise ValueError('The frozen protocol archive failed verification.')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or sum(row.file_size for row in archive.infolist())>MAX_BYTES:
            raise ValueError('Invalid protocol archive.')
        descriptor=validate_descriptor(json.loads(archive.read('protocol.json')))
        if set(names)!={'protocol.json',*descriptor['files']}:raise ValueError('Unexpected protocol sources.')
        for name in names:
            target=folder/relative_path(name);target.parent.mkdir(parents=True,exist_ok=True)
            if target.is_symlink():raise ValueError('Protocol extraction cannot replace a symlink.')
            target.write_bytes(archive.read(name))
    return descriptor


@contextmanager
def load_protocol(folder,descriptor):
    name='_tempo_evaluation_'+uuid.uuid4().hex
    spec=importlib.util.spec_from_file_location(name,Path(folder)/descriptor.get('entrypoint','protocol.py'),submodule_search_locations=[str(folder)])
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module
    try:
        spec.loader.exec_module(module)
        if not callable(getattr(module,'prepare',None)) or not callable(getattr(module,'run',None)):
            raise ValueError('Protocols must implement prepare(context) and run(context).')
        yield module
    finally:
        for key in list(sys.modules):
            if key==name or key.startswith(name+'.'):sys.modules.pop(key,None)


def write_json(path,value):
    from tempo_annotation_store import atomic_bytes
    atomic_bytes(path,json.dumps(value,indent=2,allow_nan=False).encode())


class EvaluationContext:
    """Runtime services; protocols own their models, stages, metrics and study rules."""
    def __init__(self,*,plan,stage,workspace,input_root,reports,progress,checkpoint,materialize,cancelled,engine_root=None,model_fetch=None):
        self.plan=copy.deepcopy(plan);self.stage=stage;self.workspace=Path(workspace);self.input_root=Path(input_root)
        self.parameters=copy.deepcopy(plan['parameters']);self.reports=copy.deepcopy(reports)
        self.progress=progress;self.checkpoint=checkpoint;self.materialize=materialize;self.cancelled=cancelled
        self.engine_root=Path(engine_root) if engine_root else None;self._model_fetch=model_fetch

    def check_cancelled(self):
        if self.cancelled():raise InterruptedError('Model Evaluation cancelled.')

    def freeze_selection(self,value,name='evaluation'):
        """Persist a chosen model/configuration before opening reserved evaluation data."""
        from tempo_evaluation import checksum
        target=self.workspace/'selections'/(slug(name)+'.json')
        if target.exists():
            if checksum(json.loads(target.read_text()))!=checksum(value):
                raise ValueError('The frozen evaluation selection cannot change.')
        else:write_json(target,value)
        self.checkpoint()

    def run_command(self,command,*,log='worker.log',timeout=None):
        from notebooks.colab_runtime import run_logged
        return run_logged(command,label=self.stage['label'],cwd=self.workspace,
            log_path=self.workspace/relative_path(log),cancelled=self.cancelled,timeout=timeout)

    def model_path(self,filename):
        """Fetch a model from the model-repository revision frozen with this plan."""
        if self._model_fetch is None:raise ValueError('No frozen model source is available.')
        return self._model_fetch(relative_path(filename))


def validate_report(report):
    if not isinstance(report,dict):raise ValueError('A protocol must return a report object.')
    if len(json.dumps(report,allow_nan=False).encode())>8*1024*1024:raise ValueError('Evaluation report exceeds 8 MiB.')
    for table in report.get('tables',[]):
        columns=table.get('columns',[])
        if not columns or len(columns)>50 or len(table.get('rows',[]))>100000:raise ValueError('Invalid report table dimensions.')
        if any(len(row)!=len(columns) for row in table.get('rows',[])):raise ValueError('Report rows must match their table columns.')
    for preview in report.get('previews',[]):
        if not preview.get('name') or not preview.get('views'):raise ValueError('Image comparisons need names and views.')
        for path in preview['views'].values():relative_path(path)
    decision=report.get('decision')
    if decision is not None and decision.get('passed') is not True and decision.get('passed') is not False and decision.get('passed') is not None:
        raise ValueError('Invalid evaluation decision.')
    return report
