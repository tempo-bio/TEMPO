"""Image-field holdouts, cumulative reuse, and small approved datasets."""
import json
from pathlib import Path
import tempfile
import unittest
import cv2
import numpy as np
from tempo_training_groups import plan_splits, validate_assignments
from tempo_training import build_dataset, validate_training_dataset


class TrainingGroupTests(unittest.TestCase):
    def test_explicit_preparation_group_cannot_cross_roles(self):
        names = [f'twoone-20x-{i}.png' for i in range(4)]
        assignments = plan_splits(names, {name:'same-well' for name in names})
        self.assertEqual(set(assignments.values()), {'train'})

    def test_assignments_are_deterministic_and_input_order_independent(self):
        names = [f'{i}.png' for i in range(20)]
        self.assertEqual(plan_splits(names),plan_splits(names[::-1]))
        self.assertEqual(list(plan_splits(names).values()).count('train'),14)

    def test_new_images_preserve_roles_and_fill_missing_roles(self):
        old = {'a.png':'train'}
        two = plan_splits(['a.png','b.png'], previous=old)
        self.assertEqual(two,{'a.png':'train','b.png':'test'})
        three = plan_splits(['a.png','b.png','c.png'], previous=two)
        self.assertEqual(three,dict(two,**{'c.png':'val'}))

    def test_old_training_images_never_become_holdouts(self):
        old = {f'{i}.png':'train' for i in range(4)}
        self.assertEqual(plan_splits(old,previous=old),old)
        expanded = plan_splits(list(old)+['new-a.png','new-b.png'],previous=old)
        self.assertEqual({name:expanded[name] for name in old},old)
        self.assertEqual(set(expanded.values()),{'train','val','test'})

    def test_missing_or_invalid_assignments_are_errors(self):
        for assignments in ({},{'a.png':'other'},{'a.png':'test'}):
            with self.assertRaises(ValueError):validate_assignments(['a.png'],assignments)
        with self.assertRaises(ValueError):plan_splits([])

    def test_one_two_and_four_images_compile_without_well_requirement(self):
        for count in (1,2,4):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);images=root/'datasets/images';images.mkdir(parents=True)
                records={}
                for index in range(count):
                    name=f'field-{index}.png';cv2.imwrite(str(images/name),np.zeros((32,32),np.uint8))
                    records[name]={'status':'approved','source_well':'one-well',
                                   'boxes':[{'class_id':0,'polygon':[[2,2],[20,2],[20,20]]}]}
                original=json.dumps(records);source=root/'datasets/universal_annotations.json';source.write_text(original)
                dataset=build_dataset(root);info=validate_training_dataset(dataset)
                self.assertEqual(len(info['records']),count)
                self.assertEqual(info['evaluation_partition'],'train' if count==1 else 'test')
                self.assertEqual(info['validation_partition'],'val' if count==4 else 'train')
                self.assertEqual(source.read_text(),original)
                self.assertEqual(sorted(info['source_groups']),[])
                self.assertIn('val: images/'+info['validation_partition'],(dataset/'cells/data.yaml').read_text())

    def test_trial_compilation_excludes_saved_holdouts_and_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);images=root/'datasets/images';images.mkdir(parents=True)
            records={}
            for index in range(4):
                name=f'{index}.png';cv2.imwrite(str(images/name),np.zeros((32,32),np.uint8))
                records[name]={'status':'approved','boxes':[{'class_id':0,'polygon':[[2,2],[20,2],[20,20]]}]}
            (root/'datasets/universal_annotations.json').write_text(json.dumps(records))
            assignments=plan_splits(records)
            dataset=build_dataset(root,split_assignments=assignments,training_check=True)
            info=validate_training_dataset(dataset,allow_training_check=True)
            self.assertEqual({row['name'] for row in info['records']},{name for name,part in assignments.items() if part=='train'})
            with self.assertRaises(ValueError):validate_training_dataset(dataset)
            self.assertFalse((root/'datasets/training_splits.json').exists())


if __name__ == '__main__':unittest.main()
