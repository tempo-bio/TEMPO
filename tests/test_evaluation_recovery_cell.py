"""Save-only backup generation; no training, Colab or shared services."""
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import zipfile
from notebooks.build_evaluation_recovery import build_cell,DRIVER


class RecoveryCellTests(unittest.TestCase):
    def test_cell_downloads_verified_backup_and_launches_only_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            content=Path(folder);engine=content/'engine';engine.mkdir()
            job=dict(id='b'*32,evaluation_id='a'*32)
            (engine/'notebook_settings.json').write_text(json.dumps(dict(evaluation_request=job)))
            original=b'raise AssertionError("training was executed")\n'
            (engine/'train_yolo.py').write_bytes(original)
            workspace=content/'TEMPO-evaluations'/job['evaluation_id'];workspace.mkdir(parents=True)
            (workspace/'manifest.json').write_text('{}')
            model=workspace/'results/model.pt';model.parent.mkdir();model.write_bytes(b'valuable fitted model')
            downloads=[];commands=[]
            def logged(command,**options):commands.append(command)
            files=types.SimpleNamespace(download=downloads.append)
            namespace=dict(root=engine,python='isolated-python',run_logged=logged)
            class Paths(ast.NodeTransformer):
                def visit_Constant(self,node):
                    if isinstance(node.value,str) and node.value.startswith('/content'):
                        return ast.copy_location(ast.Constant(str(content)+node.value[len('/content'):]),node)
                    return node
            code=ast.fix_missing_locations(Paths().visit(ast.parse(build_cell())))
            with patch.dict('sys.modules',{'google.colab':types.SimpleNamespace(files=files)}):
                exec(compile(code,'save-only-cell','exec'),namespace)
            self.assertEqual(len(downloads),1)
            with zipfile.ZipFile(downloads[0]) as archive:
                manifest=json.loads(archive.read('backup.json'))
                for name,row in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(),row['sha256'])
                self.assertEqual(archive.read('workspace/results/model.pt'),b'valuable fitted model')
                self.assertEqual(archive.read('engine/train_yolo.py'),original)
            self.assertEqual(commands,[['isolated-python','-u',str(engine/'save-evaluation.py')]])
            self.assertEqual((engine/'train_yolo.py').read_bytes(),original)
            self.assertEqual(model.read_bytes(),b'valuable fitted model')
            compile(DRIVER,'save-only-driver','exec')

    def test_missing_runtime_stops_before_backup_or_any_command(self):
        with self.assertRaisesRegex(RuntimeError,'existing connected runtime'):
            exec(compile(build_cell(),'save-only-cell','exec'),{})


if __name__=='__main__':unittest.main()
