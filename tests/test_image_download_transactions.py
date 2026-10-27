"""Interrupted downloads and checkout must preserve local annotation inputs."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import tempo_hub as hub_module
from tempo_hub import TempoHub


class ImageDownloadTransactionTests(unittest.TestCase):

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.images=self.root/'images'; self.images.mkdir()
        self.session=self.root/'session.json'
        for name,value in [('UNIVERSAL_DIR',str(self.images)),('LOCAL_SESSION_PATH',str(self.session))]:
            p=patch.object(hub_module,name,value); p.start(); self.addCleanup(p.stop)
        self.hub=TempoHub.__new__(TempoHub)
        self.hub._require_actor=lambda requested=None:requested or 'Alice'
        self.hub._prepare_manifest=lambda manifest,revision:(manifest,{})
        self.hub.token='synthetic'; self.hub.api=Mock()
        self.hub.get_dataset_repo=lambda:'synthetic/data'
        self.source=self.root/'download.png'; self.source.write_bytes(b'remote-image')
        p=patch.object(hub_module,'hf_hub_download',return_value=str(self.source))
        self.download=p.start(); self.addCleanup(p.stop)

    def checkout(self):
        self.hub.claim_batch=Mock(return_value=(['image.png'],[]))
        self.hub._head=Mock(return_value='revision')
        self.hub._read=Mock(side_effect=[{'images':{'image.png':TempoHub._lease('Alice')}},{},{}])
        self.hub.download_image=Mock()
        return self.hub.checkout_batch(['image.png'],'Alice')

    def test_malformed_saved_session_is_not_replaced_and_no_remote_claim_starts(self):
        for content in ('{broken','[]','{"annotations": [], "claimed_images": []}',
                        '{"annotations": {"old.png": null}, "claimed_images": []}',
                        '{"annotations": {}, "claimed_images": "old.png"}'):
            with self.subTest(content=content):
                self.session.write_text(content)
                with self.assertRaisesRegex(ValueError,'session'):
                    self.checkout()
                self.assertEqual(self.session.read_text(),content)
                self.hub.claim_batch.assert_not_called()
                self.hub.download_image.assert_not_called()

    def test_session_read_permission_failure_never_overwrites_it_or_claims_remote(self):
        self.session.write_text('{"annotations": {}}'); before=self.session.read_bytes()
        original_open=open
        def read(path,*args,**kwargs):
            if str(path)==str(self.session): raise PermissionError('synthetic denied session read')
            return original_open(path,*args,**kwargs)
        with patch('builtins.open',side_effect=read), self.assertRaises(PermissionError):
            self.checkout()
        self.assertEqual(self.session.read_bytes(),before)
        self.hub.claim_batch.assert_not_called()

    def test_valid_checkout_keeps_unrelated_draft_and_claims(self):
        old={'boxes':[{'class_id':0,'polygon':[[1,1],[2,1],[2,2]]}]}
        self.session.write_text(json.dumps({'annotations':{'old.png':old},'claimed_images':['old.png']}))
        self.assertEqual(self.checkout(),(['image.png'],[]))
        saved=json.loads(self.session.read_text())
        self.assertEqual(saved['annotations']['old.png'],old)
        self.assertEqual(saved['claimed_images'],['image.png','old.png'])

    def test_interrupted_image_copy_exposes_no_partial_file(self):
        def incomplete(source,destination):
            Path(destination).write_bytes(b'partial')
            raise OSError('synthetic full disk')
        with patch.object(hub_module.shutil,'copy2',side_effect=incomplete), self.assertRaises(OSError):
            self.hub.download_image('image.png',revision='pinned')
        self.assertFalse((self.images/'image.png').exists())
        self.assertEqual(list(self.images.glob('*.tmp')),[])

    def test_existing_different_image_is_never_replaced(self):
        target=self.images/'image.png'; target.write_bytes(b'local-annotated-image')
        with self.assertRaises(FileExistsError):
            self.hub.download_image('image.png',revision='pinned')
        self.assertEqual(target.read_bytes(),b'local-annotated-image')

    def test_existing_matching_image_is_reused_without_copy(self):
        target=self.images/'image.png'; target.write_bytes(self.source.read_bytes())
        with patch.object(hub_module.shutil,'copy2') as copy_file:
            self.assertEqual(self.hub.download_image('image.png',revision='pinned'),str(target))
        copy_file.assert_not_called()

    def test_legacy_alias_cannot_overwrite_an_unrelated_image(self):
        alias=self.images/'image.png'; alias.write_bytes(b'other-image-with-annotations')
        filename='20261008_123000_image.png'
        target=self.hub.download_image(filename,revision='pinned')
        self.assertEqual(Path(target).read_bytes(),b'remote-image')
        self.assertEqual(alias.read_bytes(),b'other-image-with-annotations')

    def test_failed_download_keeps_original_network_error_and_does_not_retry_alias(self):
        self.download.side_effect=ConnectionError('synthetic disconnected')
        with self.assertRaisesRegex(ConnectionError,'disconnected'):
            self.hub.download_image('20261008_123000_image.png',revision='pinned')
        self.download.assert_called_once()

    def test_missing_legacy_name_still_tries_clean_name_at_same_revision(self):
        self.download.side_effect=[hub_module.EntryNotFoundError('missing'),str(self.source)]
        filename='20261008_123000_image.png'
        self.assertEqual(Path(self.hub.download_image(filename,revision='pinned')).read_bytes(),b'remote-image')
        self.assertEqual([c.kwargs['filename'] for c in self.download.call_args_list],
                         ['images/'+filename,'images/image.png'])
        self.assertEqual({c.kwargs['revision'] for c in self.download.call_args_list},{'pinned'})

    def test_destination_created_during_download_is_preserved(self):
        target=self.images/'image.png'; original_link=hub_module.os.link
        def publish(source,destination):
            target.write_bytes(b'concurrent-local-image')
            return original_link(source,destination)
        with patch.object(hub_module.os,'link',side_effect=publish), self.assertRaises(FileExistsError):
            self.hub.download_image('image.png',revision='pinned')
        self.assertEqual(target.read_bytes(),b'concurrent-local-image')
        self.assertEqual(list(self.images.glob('*.tmp')),[])

    def test_publication_failure_preserves_directory_without_partial_image(self):
        with patch.object(hub_module.os,'link',side_effect=OSError('synthetic publication denied')), \
             self.assertRaises(OSError):
            self.hub.download_image('image.png',revision='pinned')
        self.assertEqual(list(self.images.iterdir()),[])

    def test_changed_copy_is_rejected_before_publication(self):
        def corrupt(source,destination): Path(destination).write_bytes(b'changed')
        with patch.object(hub_module.shutil,'copy2',side_effect=corrupt), self.assertRaisesRegex(ValueError,'changed during copying'):
            self.hub.download_image('image.png',revision='pinned')
        self.assertEqual(list(self.images.iterdir()),[])

    def test_checkout_reloads_drafts_saved_while_download_was_waiting(self):
        self.session.write_text(json.dumps({'annotations':{},'claimed_images':[]}))
        self.hub.claim_batch=Mock(return_value=(['image.png'],[]))
        self.hub._head=Mock(return_value='revision')
        self.hub._read=Mock(side_effect=[{'images':{'image.png':TempoHub._lease('Alice')}},{},{}])
        newer={'boxes':[],'sync_pending':True}
        def download(*args,**kwargs):
            self.session.write_text(json.dumps({'annotations':{'new-draft.png':newer},'claimed_images':['new-draft.png']}))
        self.hub.download_image=Mock(side_effect=download)
        self.hub.checkout_batch(['image.png'],'Alice')
        saved=json.loads(self.session.read_text())
        self.assertEqual(saved['annotations']['new-draft.png'],newer)
        self.assertIn('new-draft.png',saved['claimed_images'])


if __name__=='__main__': unittest.main()
