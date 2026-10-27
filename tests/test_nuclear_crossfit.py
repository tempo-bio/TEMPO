"""Cross-fit validation remains disjoint and reusable with partial nuclear labels."""
import json
import hashlib
import types
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import cv2

class CrossFitTests(unittest.TestCase):
    def test_positive_internal_validation_is_disjoint_and_completed_folds_are_reused(self):
        import tempo_nuclear_training as nuclear
        from tempo_preprocessing import metadata
        with tempfile.TemporaryDirectory() as directory:
            dataset=Path(directory)/'dataset';(dataset/'cells/images/train').mkdir(parents=True)
            (dataset/'nuclei/train').mkdir(parents=True)
            rows=[]
            for name in ('a','b','c','z'):
                cv2.imwrite(str(dataset/'cells/images/train'/(name+'.png')),np.zeros((16,16),np.uint8))
                labels=np.zeros((16,16),np.int32)
                if name!='z':labels[2:6,2:6]=1
                np.save(dataset/'nuclei/train'/(name+'.npy'),labels)
                rows.append(dict(stem=name,split='train',preparation=name,source_sha256=hashlib.sha256(name.encode()).hexdigest()))
            (dataset/'dataset.json').write_text(json.dumps(dict(records=rows,preprocessing=metadata('P0'),crossfit_unit='field')))
            calls=[]
            def train(dataset,output,*args,**kwargs):
                calls.append(kwargs)
                path=Path(output)/'stardist_nuclei.zip';path.write_bytes(b'fold fixture')
                return str(path)
            tensorflow=types.SimpleNamespace(keras=types.SimpleNamespace(backend=types.SimpleNamespace(clear_session=lambda:None)))
            with patch.object(nuclear,'train_nuclear_model',side_effect=train),patch('tempo_training.unpack_stardist',return_value=object()), \
                 patch.object(nuclear,'profile_images',return_value=([np.zeros((16,16),np.float32)]*4,[np.zeros((16,16),np.int32)]*4)), \
                 patch('tempo_nuclei.continuous_map',side_effect=lambda model,image:np.zeros_like(image)),patch.dict('sys.modules',{'tensorflow':tensorflow}):
                nuclear.prepare_crossfit(dataset,Path(directory)/'folds',1,1,None)
                self.assertEqual(len(calls),3)
                for call in calls:
                    self.assertFalse(call['train_stems'] & call['validation_stems'])
                    self.assertTrue(call['validation_stems'] <= {'a','b','c'})
                nuclear.prepare_crossfit(dataset,Path(directory)/'folds',1,1,None)
                self.assertEqual(len(calls),3)
