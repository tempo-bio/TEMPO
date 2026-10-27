"""Actual legacy CLI training and scoring recovery; CPU, synthetic fields, no Hub."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from tempo_cloud import BUNDLE_FILES, archive_sources
from tempo_training_runtime import REPAIRS, compatibility_files, verify_runtime_support


CLI_CHECK = '''import hashlib,json,os,shutil,sys
from pathlib import Path
import cv2,numpy as np,torch
from ultralytics import YOLO
from ultralytics.utils import USER_CONFIG_DIR
from matplotlib import font_manager
from tempo_training import build_dataset,MODEL_FILES
from notebooks.colab_runtime import run_logged
root=Path.cwd();torch.set_num_threads(2)
USER_CONFIG_DIR.mkdir(parents=True,exist_ok=True)
shutil.copyfile(font_manager.findfont('DejaVu Sans'),USER_CONFIG_DIR/'Arial.ttf')
images=root/'datasets/images';images.mkdir(parents=True);records={};splits={}
for i,split in enumerate(('train','train','val','test')):
    name=f'field{i}.png'
    cv2.imwrite(str(images/name),np.random.default_rng(i).integers(0,255,(96,96),dtype=np.uint8))
    records[name]=dict(status='approved',width=96,height=96,coordinate_space='normalized',
        image_id='sha256:'+hashlib.sha256((images/name).read_bytes()).hexdigest(),boxes=[
            dict(class_id=0,polygon=[[.1,.1],[.3,.1],[.3,.3],[.1,.3]]),
            dict(class_id=1,polygon=[[.4,.1],[.6,.1],[.6,.3],[.4,.3]]),
            dict(class_id=0,object_type='group',polygon=[[.5,.5],[.9,.5],[.9,.9],[.5,.9]])])
    splits[name]=split
(root/'datasets/universal_annotations.json').write_text(json.dumps(records))
dataset=build_dataset(root,origin='experiment',assisted=False,
    source_groups={name:name for name in records},split_assignments=splits)
torch.save(dict(model=YOLO('yolo26n.yaml').model,train_args={}),root/'yolo26n.pt')
output=root/'candidate'
command=[sys.executable,'-u',str(root/'train_yolo.py'),'--worker','nano_scout','--dataset',str(dataset),
         '--output',str(output),'--epochs','1','--batch','2','--workers','0','--device','cpu']
run_logged(command,label='Legacy fit and scoring',cwd=root,timeout=120)
artifact=output/MODEL_FILES['nano_scout'];digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
best=output/'training/weights/best.pt';mtime=best.stat().st_mtime_ns
# Reproduce the state left by the old post-export scoring failure.
(output/'result.json').unlink();(output/'trained_candidate.json').unlink()
run_logged(command,label='Legacy scoring recovery',cwd=root,timeout=60)
assert digest==hashlib.sha256(artifact.read_bytes()).hexdigest()
assert best.stat().st_mtime_ns==mtime
result=json.loads((output/'result.json').read_text())
assert result['sha256']==digest and not result['publishable']
assert json.loads((output/'trained_candidate.json').read_text())['provenance']['kind']=='legacy-final-export'
print('VERIFIED: model bytes retained, completed fitting not repeated, scoring result saved.')
'''


class RuntimeIntegrationTests(unittest.TestCase):
    def test_actual_frozen_worker_subprocess_scores_a_completed_export_without_fitting_again(self):
        repository=Path(__file__).resolve().parents[1]
        fixtures=Path(__file__).parent/'fixtures/legacy_training'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();sources={name:(repository/name).read_bytes() for name in BUNDLE_FILES}
            for name in REPAIRS:sources[name]=(fixtures/(name.replace('/','__')+'.txt')).read_bytes()
            for name,data in sources.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
            frozen=archive_sources(root)
            support_sources={'tempo_training_runtime.py':sources['tempo_training_runtime.py'],
                             'colab_runtime.py':(repository/'notebooks/colab_runtime.py').read_bytes()}
            files,_=compatibility_files(sources,support_sources)
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:
                for name,data in files.items():
                    path=root/'runtime-support'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
                    archive.writestr('runtime-support/'+name,data)
            environment=dict(os.environ,TEMPO_EXECUTION_RUNTIME=str(root),
                PYTHONPATH=os.pathsep.join([str(root/'runtime-support'),str(repository),os.environ.get('PYTHONPATH','')]),
                YOLO_CONFIG_DIR=str(root/'test-config'),OMP_NUM_THREADS='2',MKL_NUM_THREADS='2',MPLBACKEND='Agg')
            result=subprocess.run([sys.executable,'-c',CLI_CHECK],cwd=root,env=environment,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=190)
            self.assertEqual(result.returncode,0,result.stdout[-6000:])
            recovery=result.stdout.split('START: Legacy scoring recovery',1)[1]
            self.assertIn('Reusing completed scout training',recovery)
            self.assertNotIn('Starting training',recovery)
            self.assertIn('VERIFIED: model bytes retained',recovery)
            self.assertEqual(archive_sources(root),frozen)
            verify_runtime_support(root,stream.getvalue())


if __name__ == '__main__':unittest.main()
