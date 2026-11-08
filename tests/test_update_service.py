import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import zipfile
from dataclasses import replace

import tempo_update_service as service
from tests.test_update_manifest import NOW, PUBLIC, payload, signed, asset, sha, load


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def package(content=b'app'):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        z.writestr('dir/TEMPO.exe', content)
    return stream.getvalue()


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'updates'
        self.obj = payload()
        self.data = package()
        self.obj['full'] = asset('1.0.2', 'full.zip', self.data)
        self.downloads = {self.obj['full']['url']: self.data}
        self.refresh_index()
        self.update = service.UpdateService(self.root, {'test': PUBLIC}, opener=self.open, clock=lambda: NOW)

    def refresh_index(self):
        self.index = json.dumps({'releases': [json.loads(signed(self.obj))]}).encode()

    def open(self, request, timeout):
        url = request.full_url
        return Response(self.index if url == service.FEEDS['stable'] else self.downloads[url], url)

    def plan(self):
        self.update.check()
        return self.update.plan(installed_version='1.0.1', installed_commit='b'*40)

    def test_prepare_and_reverify_and_tamper(self):
        plan = self.plan()
        result = self.update.prepare(plan)
        release, path = self.update.verified_prepared()
        self.assertEqual(release.version, '1.0.2')
        self.assertEqual(Path(result['package']), path)
        data = json.loads((self.root/'prepared-release.json').read_bytes())
        data['signed']['commit'] = 'c'*40
        (self.root/'prepared-release.json').write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.update.verified_prepared()

    def test_watermark_rejects_rollback_and_same_sequence_changed(self):
        self.obj['sequence'] = 2
        self.refresh_index()
        self.update.check()
        self.obj['sequence'] = 1
        self.refresh_index()
        with self.assertRaisesRegex(ValueError, 'older'):
            self.update.check()
        self.obj['sequence'] = 2
        self.obj['commit'] = 'c'*40
        self.refresh_index()
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.update.check()

    def test_cache_corruption_replaced_and_truncation_cleans_temporary(self):
        plan = self.plan()
        cache = self.root/'packages'
        cache.mkdir(exist_ok=True)
        (cache/'full.zip').write_bytes(b'bad')
        self.update.prepare(plan)
        self.assertEqual((cache/'full.zip').read_bytes(), self.data)
        (cache/'full.zip').unlink()
        self.downloads[self.obj['full']['url']] = self.data[:-2]
        with self.assertRaises(ValueError):
            self.update.prepare(plan)
        self.assertFalse((cache/'full.zip').exists())
        self.assertEqual(list(cache.glob('.download-*')), [])

    def test_cancellation_cleans_partial(self):
        plan = self.plan()
        checks = iter([False, False, True])
        with self.assertRaises(service.UpdateCancelled):
            self.update.prepare(plan, cancel=lambda: next(checks, True))
        self.assertEqual(list((self.root/'packages').glob('.download-*')), [])
        self.assertFalse((self.root/'prepared.json').exists())

    def test_forged_plan_never_fetches(self):
        plan = self.plan()
        forged = replace(plan, assets=(replace(plan.assets[0], url='https://evil.example/payload'),))
        with self.assertRaisesRegex(ValueError, 'plan or base'):
            self.update.prepare(forged)
        self.assertEqual(list((self.root/'packages').glob('*.zip')), [])

    def test_fabricated_base_and_corrupt_base(self):
        self.update.check()
        base_obj = payload('1.0.1', 'b'*40)
        base = load(base_obj)
        archive = self.root/'base.zip'
        archive.write_bytes(package(b'bad'))
        plan = self.update.plan(installed_version='1.0.1', installed_commit='b'*40,
                                base_release=base, base_package=archive)
        self.assertEqual(plan.kind, 'full')
        with self.assertRaisesRegex(ValueError, 'signed envelope'):
            self.update.plan(installed_version='1.0.1', installed_commit='b'*40,
                             base_release=replace(base, commit='c'*40), base_package=archive)

    def test_recovery_base_download_is_budgeted_and_kept_separate_from_target(self):
        base_data = package(b'old app')
        base_obj = payload('1.0.1', 'b'*40)
        base_obj['inventory'][0].update(size=len(b'old app'), sha256=sha(b'old app'))
        base_obj['full'] = asset('1.0.1', 'full.zip', base_data)
        self.obj['sequence'] = base_obj['sequence'] + 1
        self.index = json.dumps({'releases': [json.loads(signed(obj)) for obj in (base_obj, self.obj)]}).encode()
        self.downloads[base_obj['full']['url']] = base_data
        base, _ = self.update.check()
        plan = self.update.plan(installed_version=base.version, installed_commit=base.commit, base_release=base)
        without_base = 2 * (len(b'app') + len(self.data))
        with patch.object(service.shutil, 'disk_usage', return_value=type('Disk', (), {'free': without_base})()):
            with self.assertRaisesRegex(OSError, 'Insufficient free space'):
                self.update.prepare(plan, base_release=base)
        self.assertFalse(self.update.recovery_package_path(base).exists())
        progress = []
        self.update.prepare(plan, base_release=base, progress=lambda received, total: progress.append((received, total)))
        self.assertEqual(progress[-1], (len(base_data) + len(self.data),) * 2)
        self.assertEqual(self.update.recovery_package_path(base).read_bytes(), base_data)
        self.assertEqual(self.update.verified_prepared()[1].read_bytes(), self.data)

    def test_patch_output_is_verified_and_invalid_reconstruction_removed(self):
        from tempo_update_manifest import inventory_digest
        base_obj = payload('1.0.1', 'b'*40)
        base = load(base_obj)
        base_zip = self.root/'base.zip'
        base_zip.write_bytes(package())
        delta = b'patch'
        self.obj['patches'] = [{'base_version': base.version, 'base_commit': base.commit,
                                'base_inventory_sha256': inventory_digest(base.inventory),
                                'target_version': '1.0.2', 'target_commit': self.obj['commit'],
                                'asset': asset('1.0.2', 'patch.zip', delta)}]
        self.downloads[self.obj['patches'][0]['asset']['url']] = delta
        self.refresh_index()
        self.update.check()
        plan = self.update.plan(installed_version='1.0.1', installed_commit='b'*40,
                                base_release=base, base_package=base_zip)
        self.assertEqual(plan.kind, 'patch')
        with patch.object(self.update, 'opener', side_effect=URLError('missing delta')):
            with self.assertRaises(service.FullDownloadRequired):
                self.update.prepare(plan, base_release=base, base_package=base_zip)
        self.downloads[self.obj['patches'][0]['asset']['url']] = b'wrong checksum'
        with self.assertRaises(service.FullDownloadRequired):
            self.update.prepare(plan, base_release=base, base_package=base_zip)
        self.downloads[self.obj['patches'][0]['asset']['url']] = delta
        patcher = self.root/'patcher'
        patcher.write_bytes(b'fixture')

        class FakeProcess:
            returncode = 0
            def __init__(self, command, **kwargs):
                Path(command[-1]).write_bytes(package(b'bad'))
            def poll(self):
                return 0

        with patch.object(service.subprocess, 'Popen', FakeProcess):
            with self.assertRaises(service.FullDownloadRequired):
                self.update.prepare(plan, base_release=base, base_package=base_zip,
                                    patcher=patcher, patcher_sha256=sha(b'fixture'))
        self.assertEqual(list((self.root/'packages').glob('prepared-*')), [])
        self.assertFalse((self.root/'prepared.json').exists())

    def test_base_change_after_planning_stops_patch(self):
        from tempo_update_manifest import inventory_digest
        base = load(payload('1.0.1', 'b'*40))
        base_zip = self.root/'base.zip'
        base_zip.write_bytes(package())
        self.obj['patches'] = [{'base_version': base.version, 'base_commit': base.commit,
                                'base_inventory_sha256': inventory_digest(base.inventory),
                                'target_version': '1.0.2', 'target_commit': self.obj['commit'],
                                'asset': asset('1.0.2', 'patch.zip', b'patch')}]
        self.refresh_index()
        self.update.check()
        plan = self.update.plan(installed_version='1.0.1', installed_commit='b'*40,
                                base_release=base, base_package=base_zip)
        self.assertEqual(plan.kind, 'patch')
        base_zip.write_bytes(package(b'bad'))
        with self.assertRaisesRegex(ValueError, 'verified base'):
            self.update.prepare(plan, base_release=base, base_package=base_zip)
        self.assertEqual(list((self.root/'packages').glob('*.zip')), [])

    def test_full_package_contents_checked_even_if_asset_hash_valid(self):
        self.data = package(b'bad')
        self.obj['full'] = asset('1.0.2', 'full.zip', self.data)
        self.downloads[self.obj['full']['url']] = self.data
        self.refresh_index()
        with self.assertRaises(ValueError):
            self.update.prepare(self.plan())
        self.assertFalse((self.root/'prepared.json').exists())


if __name__ == '__main__':
    unittest.main()
