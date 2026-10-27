"""Invalid leases and manifests cannot authorize writes or replace valid caches."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
import tempo_hub as hub_module
from tempo_hub import TempoHub


class CollaborationManifestValidationTests(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hub = TempoHub.__new__(TempoHub)
        self.hub._require_actor=lambda requested=None:requested or 'Alice'
        self.hub.authenticated_account=lambda:'Alice'
        self.hub._prepare_manifest=lambda manifest,revision:(manifest,{})
        self.hub._head = Mock(return_value='pinned-revision')
        self.cache = self.root/'cache.json'
        for name, value in [('LOCAL_LOCK_CACHE',str(self.cache)), ('LOCAL_SESSION_PATH',str(self.root/'session.json'))]:
            patcher = patch.object(hub_module,name,value); patcher.start(); self.addCleanup(patcher.stop)

    def test_utc_z_leases_have_same_ownership_on_supported_python_versions(self):
        lease=TempoHub._lease('Alice')
        lease['expires_at']=lease['expires_at'].replace('+00:00','Z')
        manifest={'images':{'a.png':lease}}
        self.assertEqual(hub_module.validate_lock_manifest(manifest),manifest)
        self.assertTrue(TempoHub._owned(lease,'Alice'))
        self.assertFalse(TempoHub._owned(lease,'Bob'))
        lease['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace('+00:00','Z')
        self.assertFalse(TempoHub._owned(lease,'Alice'))

    def test_malformed_owner_or_expiry_cannot_be_treated_as_an_expired_adoptable_lease(self):
        for changes in ({'claimed_by':None},{'expires_at':None}, {'expires_at':'bad'},
                        {'expires_at':'2026-10-08T12:00:00'}):
            with self.subTest(changes=changes):
                lease=dict(TempoHub._lease('Alice'),**changes)
                self.hub._read=Mock(return_value={'images':{'a.png':lease}})
                self.hub.api=Mock(); self.hub.token='synthetic'
                with self.assertRaisesRegex(ValueError,'reservation manifest'):
                    self.hub.claim_batch(['a.png'],'Bob')
                self.hub.api.create_commit.assert_not_called()

    def test_checkout_image_matches_manifest_and_draft_revision(self):
        self.hub.claim_batch = Mock(return_value=(['image.png'],[]))
        manifest = {'images':{'image.png':TempoHub._lease('Alice')}}
        self.hub._read = Mock(side_effect=[manifest,{},{}])
        self.hub.download_image = Mock()
        self.hub.checkout_batch(['image.png'],'Alice')
        self.hub.download_image.assert_called_once_with('image.png',revision='pinned-revision')

    def test_malformed_cached_manifests_are_not_returned_or_rewritten(self):
        for value in ([], {'images':[]}, {'images':{'a.png':None}}, {'images':{'a.png':{'status':'typo'}}}):
            with self.subTest(value=value):
                self.cache.write_text(json.dumps(value)); before=self.cache.read_bytes()
                self.assertEqual(self.hub.cached_lock_manifest(),{'version':1,'images':{}})
                self.assertEqual(self.cache.read_bytes(),before)

    def test_invalid_remote_manifest_preserves_good_cache_and_force_read_reports_error(self):
        good={'version':1,'images':{'a.png':{'status':'available'}}}
        self.cache.write_text(json.dumps(good)); before=self.cache.read_bytes()
        self.hub._read=Mock(return_value={'images':[]})
        with self.assertRaisesRegex(ValueError,'reservation manifest'):
            self.hub.fetch_lock_manifest(force_refresh=True)
        self.assertEqual(self.hub.fetch_lock_manifest(),good)
        self.assertEqual(self.cache.read_bytes(),before)

    def test_invalid_remote_manifest_blocks_transactions_before_any_write(self):
        self.hub._read=Mock(return_value={'images':{'a.png':None}})
        self.hub.api=Mock(); self.hub.token='synthetic'
        action=Mock(return_value=({},True))
        with self.assertRaisesRegex(ValueError,'reservation manifest'):
            self.hub._transaction(action,'synthetic action')
        action.assert_not_called(); self.hub.api.create_commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
