"""Evaluation protocol files for isolated runner and notebook tests."""
import json
from pathlib import Path


def protocol(folder,stages=('calibrate','measure'),images=True,parameters=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    descriptor=dict(format='tempo-evaluation-protocol-v1',id='developer-comparison',version='2',name='Developer comparison',
        entrypoint='protocol.py',files=['protocol.py'],stages=[dict(id=name,label=name.capitalize()) for name in stages],
        parameters=parameters or [dict(name='repeats',label='Repeats',type='integer',minimum=1,maximum=10,default=3)])
    (folder/'protocol.json').write_text(json.dumps(descriptor))
    (folder/'protocol.py').write_text('''from tempo_training_groups import plan_splits
def prepare(context):
    assignments=plan_splits(context.records,previous=context.previous_splits) if context.records else {}
    return dict(scope='Developer-defined units',images={name:dict(split=role,unit=name) for name,role in assignments.items()} if IMAGES else {})
def run(context):
    context.check_cancelled()
    folder=context.workspace/'results';folder.mkdir(exist_ok=True)
    (folder/(context.stage['id']+'.onnx')).write_bytes(b'custom model artifact')
    context.progress('Custom operation',phase='protocol-metadata',stage='custom-detail')
    if context.stage['id']==context.plan['stages'][-1]['id']:
        assert all(context.reports[stage['id']] for stage in context.plan['stages'][:-1])
        for stage in context.plan['stages'][:-1]:
            path=folder/(stage['id']+'.onnx');context.materialize(path)
            assert path.read_bytes()==b'custom model artifact'
        context.freeze_selection({'weights':'frozen-choice'})
    context.checkpoint()
    return dict(summary='Custom latency comparison',tables=[dict(title='Latency',columns=['Condition','Milliseconds'],rows=[['reference',12.],['candidate',9.]])],decision=dict(passed=True))
'''.replace('IMAGES','True' if images else 'False'))
    return folder/'protocol.json'
