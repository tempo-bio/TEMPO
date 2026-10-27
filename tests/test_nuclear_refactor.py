"""Regression coverage for partial nuclei, cell identity and centroid scouting."""
import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import torch
from tempo_annotations import normalize_record, validate_annotations
from tempo_nuclei import (attach_supplement, cell_digest, continuous_map, helper_channels,
                          is_nucleus, migrate_record, nuclear_labels, supplement)
from tempo_preprocessing import preprocess


def rectangle(cid,x,y,w,h,**extra):
    return dict(class_id=cid,polygon=[[x,y],[x+w,y],[x+w,y+h],[x,y+h]],**extra)


class NuclearSupervisionTests(unittest.TestCase):
    def record(self):
        return dict(width=100,height=80,coordinate_space='normalized',status='approved',image_id='sha256:'+'a'*64,
            nuclear_status='approved',boxes=[rectangle(0,.1,.1,.65,.65),rectangle(1,.8,.15,.15,.3),
                rectangle(2,.2,.2,.08,.08,object_type='nucleus'),
                rectangle(2,.4,.2,.08,.08,object_type='nucleus'),
                rectangle(2,.55,.4,.08,.08,object_type='nucleus')])

    def test_multiple_nuclei_do_not_change_cell_count_or_unknown_interiors(self):
        record=self.record()
        self.assertEqual(validate_annotations({'image.png':record}),(2,1,1))
        labels=nuclear_labels(record,(80,100))
        self.assertEqual(set(np.unique(labels)),{-1,0,1,2,3})
        self.assertEqual(labels[42,25],-1)
        self.assertEqual(labels[20,85],-1)  # The cell with no nuclear outlines is unknown.
        self.assertEqual(labels[0,0],0)

    def test_unapproved_nuclei_are_never_positive_training_targets(self):
        record=self.record();record['nuclear_status']='draft'
        self.assertEqual(set(np.unique(nuclear_labels(record,(80,100)))),{-1,0})

    def test_legacy_background_is_removed_without_relabeling_nuclei(self):
        record=self.record();record['boxes'].append(rectangle(2,0,0,.05,.05))
        migrated=normalize_record(record,100,80)
        self.assertEqual(len(migrated['boxes']),5)
        self.assertEqual(migrate_record(migrated),migrated)
        self.assertEqual(len(record['boxes']),6)

    def test_supplement_changes_do_not_change_approved_cell_geometry(self):
        record=self.record();original=cell_digest(record)
        nuclear=supplement(record,status='approved')
        cells=copy.deepcopy(record);cells['boxes']=cells['boxes'][:2]
        attached=attach_supplement(cells,nuclear)
        self.assertEqual(cell_digest(attached),original)
        self.assertEqual(attached['nuclear_status'],'approved')
        cells['boxes'][0]['polygon'][0][0]=.11
        self.assertEqual(attach_supplement(cells,nuclear)['nuclear_status'],'needs_review')
        nuclear['image_id']='sha256:'+'b'*64
        with self.assertRaises(ValueError):attach_supplement(cells,nuclear)

    def test_outside_nuclei_and_overlapping_nuclear_instances_fail_review(self):
        record=self.record();record['boxes'].append(rectangle(2,.01,.01,.04,.04,object_type='nucleus'))
        with self.assertRaisesRegex(ValueError,'beyond'):nuclear_labels(record,(80,100))
        record=self.record();record['boxes'].append(copy.deepcopy(record['boxes'][2]))
        with self.assertRaisesRegex(ValueError,'overlap'):nuclear_labels(record,(80,100))

    def test_helper_grid_alignment_on_odd_image_sizes_and_weak_values(self):
        model=types.SimpleNamespace(config=types.SimpleNamespace(grid=(2,2)),
            predict=lambda image:(np.array([[.01,.02,.03],[.04,.05,.06]],np.float32),None))
        cue=continuous_map(model,np.zeros((3,5),np.float32))
        self.assertEqual(cue.shape,(3,5))
        self.assertAlmostEqual(float(cue[2,4]),.06)
        stacked=helper_channels(np.zeros_like(cue),cue)
        np.testing.assert_array_equal(stacked[...,1],cue)
        self.assertLess(stacked[...,1].max(),.1)

    def test_preprocessing_is_deterministic_and_constant_safe(self):
        raw=np.arange(8000,dtype=np.uint16).reshape(80,100);before=raw.copy()
        for profile in ('P0','P1','P2'):
            first=preprocess(raw,profile)
            np.testing.assert_array_equal(first,preprocess(raw,profile))
            self.assertEqual(first.dtype,np.float32)
            self.assertTrue(np.isfinite(first).all())
            self.assertEqual(preprocess(np.zeros_like(raw),profile).sum(),0)
        np.testing.assert_array_equal(raw,before)

    def test_helper_dropout_is_scoped_to_training_and_only_nuclear_channel(self):
        from tempo_cellpose import helper_dropout
        net=torch.nn.Identity();inputs=torch.ones((32,3,5,5))
        with helper_dropout(net):
            net.train();values=net(inputs)
            self.assertTrue(torch.equal(values[:,0],inputs[:,0]))
            self.assertTrue(torch.equal(values[:,2],inputs[:,2]))
            self.assertTrue(torch.any(values[:,1]!=1))
            net.eval();self.assertTrue(torch.equal(net(inputs),inputs))
        net.train();self.assertTrue(torch.equal(net(inputs),inputs))


class ScoutAndExperimentTests(unittest.TestCase):
    def test_negative_cell_gradients_are_ignored_but_group_and_positive_gradients_remain(self):
        from tempo_scout_training import classification_weights
        scores=torch.zeros((1,3,4),requires_grad=True)
        target=torch.zeros_like(scores);target[0,1,0]=1
        inside=torch.tensor([[True,True,False]])
        loss=torch.nn.functional.binary_cross_entropy_with_logits(scores,target,reduction='none')
        (loss*classification_weights(target,inside)).sum().backward()
        self.assertEqual(scores.grad[0,0,0],0)
        self.assertNotEqual(scores.grad[0,0,2],0)
        self.assertNotEqual(scores.grad[0,1,0],0)
        self.assertNotEqual(scores.grad[0,2,0],0)

    def test_centroid_compilation_separates_nuclear_and_group_targets(self):
        from tempo_training import build_dataset
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'datasets/images').mkdir(parents=True)
            image=np.arange(8000,dtype=np.uint8).reshape(80,100)
            cv2.imwrite(str(root/'datasets/images/a.png'),image)
            record=NuclearSupervisionTests().record()
            record['boxes'].append(rectangle(0,.78,.6,.15,.2,object_type='group'))
            (root/'datasets/universal_annotations.json').write_text(json.dumps({'a.png':record}))
            dataset=build_dataset(root,assisted=False)
            lines=next((dataset/'detect/labels/train').glob('*.txt')).read_text().splitlines()
            self.assertEqual([line.split()[0] for line in lines],['0','1','2'])
            first=list(map(float,lines[0].split()[1:]))
            self.assertAlmostEqual(first[2],16/640)
            labels=np.load(next((dataset/'nuclei/train').glob('*.npy')))
            self.assertEqual(labels.max(),3)
            self.assertTrue((dataset/'cells/images/train').is_dir())
            self.assertFalse((dataset/'segment').exists())

    def test_cell_center_matching_is_class_aware_and_one_to_one(self):
        from tempo_evaluation_metrics import center_counts
        truth=[dict(class_id=0,center=[10,10]),dict(class_id=1,center=[15,10])]
        predicted=[dict(class_id=0,center=[10,10]),dict(class_id=0,center=[11,10])]
        self.assertEqual(center_counts(truth,predicted),(1,1,1))

    def test_bootstrap_samples_preparations_not_cells(self):
        from tempo_evaluation_metrics import bootstrap_difference
        rows=[dict(preparation='a',candidate={'f1':.6},control={'f1':.5}) for _ in range(100)]
        rows.append(dict(preparation='b',candidate={'f1':.3},control={'f1':.5}))
        self.assertAlmostEqual(bootstrap_difference(rows,'f1')['mean'],-.05)

    def test_nuclear_proposals_never_replace_cells_or_duplicate_nuclei(self):
        from tempo_gui_assistance import merge_nuclear_proposals
        cell=rectangle(0,5,5,50,50);nucleus=rectangle(2,15,15,8,8,object_type='nucleus')
        another=rectangle(2,35,15,8,8,object_type='nucleus')
        result=merge_nuclear_proposals([cell,nucleus],[nucleus,another],100,80)
        self.assertEqual(len(result),3)
        self.assertEqual(result[0],cell)
        self.assertTrue(result[2]['proposal'])


class NuclearTransactionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_collaboration_lifecycle import HubTests
        HubTests.setUp(self)
        self.hub.profile_name = lambda: 'Fixture'
        self.hub.is_maintainer = lambda **kwargs: True
        self.record = NuclearSupervisionTests().record()
        self.record['boxes'] = self.record['boxes'][:2]
        self.repo.files['universal_annotations.json'] = {'image.png':copy.deepcopy(self.record)}
        self.repo.files['lock_manifest.json']['images'] = {
            'image.png':dict(status='approved',image_id=self.record['image_id'],sha256='a'*64,remote_filename='image.png')}

    def test_nuclear_approval_is_atomic_and_leaves_cells_immutable(self):
        before = copy.deepcopy(self.repo.files['universal_annotations.json'])
        record = self.hub.reserve_nuclear_annotations('image.png')
        expected = record.pop('nuclear_expected')
        record['boxes'].extend(NuclearSupervisionTests().record()['boxes'][2:])
        saved = self.hub.save_nuclear_annotations('image.png',record,expected=expected)
        with self.assertRaisesRegex(ValueError,'changed'):
            self.hub.save_nuclear_annotations('image.png',record,expected=expected)
        self.hub.save_nuclear_annotations('image.png',record,submit=True,expected=saved)
        sid = self.repo.files['lock_manifest.json']['nuclear_leases']['image.png']['submission_id']
        self.hub._review(sid)
        self.assertEqual(self.repo.files['universal_annotations.json'],before)
        nuclear = self.repo.files['nuclear_annotations.json'][record['image_id']]
        self.assertEqual(nuclear['status'],'approved')
        self.assertEqual(len(nuclear['nuclei']),3)
        self.assertEqual(self.repo.files['lock_manifest.json']['images']['image.png']['status'],'approved')
        catalog=self.hub.fetch_image_catalog()
        self.assertEqual(len([shape for shape in catalog['records']['image.png']['boxes'] if is_nucleus(shape)]),3)
        reopened=self.hub.reserve_nuclear_annotations('image.png')
        self.assertEqual(reopened['nuclear_status'],'approved')
        self.assertIsNone(reopened['nuclear_expected'])
        self.assertEqual(len([shape for shape in reopened['boxes'] if is_nucleus(shape)]),3)

    def test_local_approval_splits_nuclear_layer_from_immutable_cells(self):
        record=NuclearSupervisionTests().record()
        path=Path(self.tmp.name)/'local.png';path.write_bytes(b'fixture image bytes')
        self.repo.list_repo_files=lambda **kwargs:[]
        self.hub.approve_local_images({'local.png':record},{'local.png':str(path)},'FixtureReviewer')
        saved=self.repo.files['universal_annotations.json']['local.png']
        self.assertEqual(len(saved['boxes']),2)
        nuclear=self.repo.files['nuclear_annotations.json'][saved['image_id']]
        self.assertEqual(len(nuclear['nuclei']),3)
        self.assertEqual(nuclear['status'],'approved')

    def test_nuclear_draft_cannot_change_an_approved_cell(self):
        record = self.hub.reserve_nuclear_annotations('image.png')
        record['boxes'][0]['polygon'][0][0] += .01
        with self.assertRaisesRegex(ValueError,'cell geometry'):
            self.hub.save_nuclear_annotations('image.png',record)

    def test_competing_collaborator_cannot_claim_active_nuclear_lease(self):
        self.hub.reserve_nuclear_annotations('image.png')
        self.hub._require_actor = lambda requested=None:'OtherFixture'
        with self.assertRaisesRegex(ValueError,'another collaborator'):
            self.hub.reserve_nuclear_annotations('image.png')

    def test_expired_nuclear_lease_can_be_renewed_without_changing_the_draft(self):
        record=self.hub.reserve_nuclear_annotations('image.png')
        saved=self.hub.save_nuclear_annotations('image.png',record)
        lease=self.repo.files['lock_manifest.json']['nuclear_leases']['image.png']
        lease['expires_at']='2000-01-01T00:00:00+00:00'
        with self.assertRaisesRegex(ValueError,'changed'):
            self.hub.renew_nuclear_annotations('image.png',expected=None)
        self.hub.renew_nuclear_annotations('image.png',expected=saved)
        self.assertEqual(self.repo.files['nuclear_drafts.json'][record['image_id']],saved)
        self.assertNotEqual(self.repo.files['lock_manifest.json']['nuclear_leases']['image.png']['expires_at'],lease['expires_at'])
