"""Reusable recipes preserve legacy pixels and independent model inputs."""
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
import cv2
import numpy as np
from tempo_preprocessing import PROFILES, metadata, preprocess


class PreprocessingTests(unittest.TestCase):
    def test_legacy_recipes_remain_pixel_and_metadata_identical(self):
        path=Path(__file__).parent/'fixtures/legacy_training/tempo_preprocessing.py.txt'
        legacy=types.ModuleType('legacy_preprocessing')
        exec(compile(path.read_bytes(),str(path),'exec'),legacy.__dict__)
        for dtype in (np.uint8,np.uint16):
            image=np.random.default_rng(17).integers(0,255,(65,81),dtype=dtype)
            for profile in ('P0','P1','P2'):
                self.assertEqual(metadata(profile),legacy.metadata(profile))
                np.testing.assert_array_equal(preprocess(image,profile),legacy.preprocess(image,profile))

    def test_all_recipes_are_finite_deterministic_nonmutating_and_constant_safe(self):
        image=np.random.default_rng(4).integers(0,65535,(65,81),dtype=np.uint16);before=image.copy()
        for profile in PROFILES:
            result=preprocess(image,profile)
            self.assertEqual(result.shape,image.shape);self.assertEqual(result.dtype,np.float32)
            self.assertTrue(np.isfinite(result).all());self.assertGreaterEqual(result.min(),0);self.assertLessEqual(result.max(),1)
            np.testing.assert_array_equal(result,preprocess(image,profile))
            self.assertEqual(preprocess(np.ones((4,5)),profile).sum(),0)
        np.testing.assert_array_equal(image,before)

    def test_gaussian_reduces_fine_noise_and_order_is_explicit(self):
        image=np.random.default_rng(7).normal(100,15,(96,96)).astype(np.float32)
        self.assertLess(preprocess(image,'gaussian1').var(),preprocess(image,'P0').var())
        self.assertEqual(metadata('gaussian1-clahe2')['order'],['percentile_normalization','gaussian','clahe'])
        self.assertEqual(metadata('clahe4')['order'],['percentile_normalization','clahe'])
        self.assertGreater(np.count_nonzero(preprocess(image,'P1')!=preprocess(image,'clahe4')),100)
        for image in (np.zeros((2,2,2,2)),np.array([[np.nan]]),np.array([[np.inf]])):
            with self.assertRaises(ValueError):preprocess(image,'gaussian1')

    def test_distinct_scout_recipe_is_compiled_and_receipt_checked_independently(self):
        from tempo_training import build_dataset,read_image_plane
        from tempo_evaluation_metrics import validation_allows
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);images=root/'datasets/images';images.mkdir(parents=True)
            records={}
            for i in range(4):
                image=np.random.default_rng(i).integers(0,255,(48,64),dtype=np.uint8)
                name=f'{i}.png';cv2.imwrite(str(images/name),image)
                records[name]=dict(status='approved',width=64,height=48,coordinate_space='normalized',boxes=[
                    dict(class_id=0,polygon=[[.1,.1],[.4,.1],[.4,.4],[.1,.4]]),
                    dict(class_id=1,polygon=[[.6,.6],[.9,.6],[.9,.9],[.6,.9]])])
            (root/'datasets/universal_annotations.json').write_text(json.dumps(records))
            (root/'datasets/architecture_validation.json').write_text(json.dumps(dict(profile='P0',scout_profile='gaussian1-clahe2',nuclear_assistance_passed=False)))
            dataset=build_dataset(root)
            info=json.loads((dataset/'dataset.json').read_text())
            self.assertEqual(info['scout_preprocessing']['profile'],'gaussian1-clahe2')
            for row in info['records']:
                raw=read_image_plane(images/row['name'])
                stem=row['stem']+'.png';split=row['split']
                np.testing.assert_array_equal(read_image_plane(dataset/'detect/images'/split/stem),np.rint(preprocess(raw,'gaussian1-clahe2')*255).astype(np.uint8))
                np.testing.assert_array_equal(read_image_plane(dataset/'cells/images'/split/stem),np.rint(preprocess(raw,'P0')*255).astype(np.uint8))
        receipt=dict(format='tempo-experiment-validation-v1',profile='P0',scout_profile='gaussian1-clahe2',centroid_scout_passed=True,nuclear_assistance_passed=True,box_width=16,confirmation_sha256='a'*64)
        self.assertTrue(validation_allows(receipt,'P0','cellpose'))
        self.assertTrue(validation_allows(receipt,'gaussian1-clahe2','nano_scout',16))
        self.assertFalse(validation_allows(receipt,'P0','nano_scout',16))
