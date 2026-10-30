"""Real selection/hover behavior and isolated administrator/profile transactions."""
import copy
from contextlib import contextmanager
from tests.hub_http_fixture import httpx
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from unittest.mock import Mock, patch
from huggingface_hub.errors import HfHubHTTPError

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFormLayout
from tempo_config import TempoConfig, DEFAULT_PREFERENCES, get_collaboration_dir
from tempo_hub import TempoHub, validate_lock_manifest
from tempo_tasks import dispose_closed_views
from tempo_shared_settings import (CONTROL_REPO, CONTROL_FILE,
    SHARED_KEYS, fetch_shared_settings, apply_shared_settings, publish_shared_settings,
    validate_settings, is_workspace_admin)
from tests.test_collaboration_lifecycle import MemoryRepo
from tests.presentation_fixtures import write_historical_dataset

APP = QApplication.instance() or QApplication([])
with patch('platform.machine', return_value='x86_64'):
    import tempo_gui as gui


def shared_values():
    return {key: DEFAULT_PREFERENCES['collaboration'][key]['value'] for key in SHARED_KEYS}


def close_fixture(view):
    view.close()
    dispose_closed_views(view)


class SharedWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.preferences = object.__new__(TempoConfig)
        self.preferences._preferences = copy.deepcopy(DEFAULT_PREFERENCES)
        self.preferences.save_to_disk = Mock(return_value=True)
        self.pref_patch = patch('tempo_shared_settings.tempo_preferences', self.preferences)
        self.pref_patch.start(); self.addCleanup(self.pref_patch.stop)
        self.hub = TempoHub('fixture-token')
        self.hub.api = Mock()
        self.hub.api.whoami.return_value = dict(id='synthetic-admin-id', name='fixture-admin',
            orgs=[dict(name=CONTROL_REPO.split('/')[0], roleInOrg='admin')])
        self.hub.api.repo_info.return_value = SimpleNamespace(sha='pinned-revision', private=True)
        self.document = dict(schema_version=2, settings=shared_values(), updated_by='hf:synthetic-admin-id')
        self.path = self.root/'settings.json'; self.path.write_text(json.dumps(self.document))
        self.session = Mock()
        self.session.get.side_effect = lambda url, **kwargs: httpx.Response(
            200, content=self.path.read_bytes(), request=httpx.Request('GET', url))
        @contextmanager
        def stream(method, url, **kwargs):
            response = self.session.get(url, **kwargs)
            try:
                yield response
            finally:
                response.close()
        self.session.stream.side_effect = stream
        session_patch = patch('tempo_shared_settings.get_session', return_value=self.session)
        session_patch.start(); self.addCleanup(session_patch.stop)

    def test_other_organization_admin_cannot_publish_or_gain_edit_access(self):
        self.hub.api.whoami.return_value['orgs'] = [dict(name='another-org', roleInOrg='admin')]
        self.assertFalse(is_workspace_admin(self.hub))
        with self.assertRaises(PermissionError):
            publish_shared_settings(self.hub, shared_values())
        self.hub.api.create_commit.assert_not_called()

    def test_trusted_source_does_not_follow_locally_changed_destinations(self):
        self.preferences.set('collaboration', 'hf_models_repo', 'other/models')
        result = fetch_shared_settings(self.hub)
        self.assertEqual(result['revision'], 'pinned-revision')
        self.assertIn(CONTROL_REPO, self.session.get.call_args.args[0])
        self.assertIn('pinned-revision', self.session.get.call_args.args[0])
        self.assertIn('Bearer fixture-token', self.session.get.call_args.kwargs['headers']['authorization'])

    def test_startup_trace_records_received_and_validated_settings_without_content(self):
        import tempo_startup_diagnostics as diagnostics
        report_path = self.root/'startup.json'
        with patch.object(diagnostics, '_recorder', diagnostics.StartupRecorder(report_path)):
            result = fetch_shared_settings(self.hub)
        self.assertEqual(result['settings'], shared_values())
        report = json.loads(report_path.read_text())
        stages = [event['stage'] for event in report['events']]
        self.assertIn('settings.metadata.completed', stages)
        self.assertIn('settings.document.validated', stages)
        response = next(event for event in report['events'] if event['stage'] == 'settings.download.response')
        self.assertEqual(response['http_status'], 200)
        self.assertEqual(response['count'], self.path.stat().st_size)
        self.assertNotIn('fixture-token', report_path.read_text())
        self.assertNotIn('synthetic-admin-id', report_path.read_text())

    def test_startup_trace_does_not_claim_application_after_save_failure(self):
        import update_wizard as updates
        import tempo_startup_diagnostics as diagnostics
        for failure in (None, OSError('private error detail')):
            with self.subTest(failed=failure is not None):
                path = self.root/'apply.json'
                with patch.object(diagnostics, '_recorder', diagnostics.StartupRecorder(path)), \
                     patch.object(updates, 'tempo_hub', self.hub), \
                     patch.object(updates, 'run_background'), \
                     patch.object(updates, 'apply_shared_settings', side_effect=failure):
                    check = updates.StartupUpdateCheck()
                    check.received('workspace', (self.hub.token, {'settings': shared_values()}), None)
                stages = [event['stage'] for event in json.loads(path.read_text())['events']]
                self.assertIn('settings.apply.started', stages)
                self.assertEqual('settings.apply.completed' in stages, failure is None)
                self.assertEqual('settings.refresh.failed' in stages, failure is not None)
                self.assertNotIn('private error detail', path.read_text())

    def test_models_can_use_the_read_only_settings_organization(self):
        self.document['settings']['hf_models_repo'] = 'tempo-bio/tempo-models'
        self.path.write_text(json.dumps(self.document))
        settings = dict(shared_values(), hf_models_repo='tempo-bio-settings/tempo-models')
        self.assertEqual(validate_settings(settings), settings)
        publish_shared_settings(self.hub, settings)
        payload = self.hub.api.create_commit.call_args.kwargs['operations'][0].path_or_fileobj
        self.assertEqual(json.loads(payload)['settings']['hf_models_repo'], 'tempo-bio-settings/tempo-models')

    def test_data_repository_must_stay_in_its_declared_organization(self):
        settings = dict(shared_values(), hf_dataset_repo='another-org/data')
        with self.assertRaisesRegex(ValueError, 'data organization'):
            validate_settings(settings)

    def test_public_control_repository_is_rejected_before_any_download(self):
        self.hub.api.repo_info.return_value.private = False
        with self.assertRaisesRegex(PermissionError, 'private'):
            fetch_shared_settings(self.hub)
        self.session.get.assert_not_called()
        self.preferences.save_to_disk.assert_not_called()

    def test_missing_settings_file_is_optional_but_inaccessible_repository_is_not(self):
        self.session.get.side_effect = lambda url, **kwargs: httpx.Response(
            404, headers={'X-Error-Code': 'EntryNotFound'}, request=httpx.Request('GET', url))
        self.assertIsNone(fetch_shared_settings(self.hub))
        self.session.get.side_effect = lambda url, **kwargs: httpx.Response(
            401, request=httpx.Request('GET', url))
        with self.assertRaises(HfHubHTTPError):
            fetch_shared_settings(self.hub)

    def test_oversized_settings_document_is_rejected(self):
        self.session.get.side_effect = lambda url, **kwargs: httpx.Response(
            200, content=b' ' * 65537, request=httpx.Request('GET', url))
        with self.assertRaisesRegex(ValueError, 'too large'):
            fetch_shared_settings(self.hub)

    def test_settings_size_limit_stops_streaming_before_reading_the_entire_body(self):
        blocks = []
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(100):
                    blocks.append(8192)
                    yield b' ' * 8192
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body())))
        with client, patch('tempo_shared_settings.get_session', return_value=client):
            with self.assertRaisesRegex(ValueError, 'too large'):
                fetch_shared_settings(self.hub)
        self.assertEqual(sum(blocks), 65536+8192)

    def test_new_organization_admin_needs_no_identity_constant(self):
        self.hub.api.whoami.return_value['id'] = 'new-admin-id'
        self.hub.api.whoami.return_value['name'] = 'new-admin'
        self.assertTrue(is_workspace_admin(self.hub))
        publish_shared_settings(self.hub, dict(shared_values(), hf_models_repo='tempo-bio/next-models'))
        payload = self.hub.api.create_commit.call_args.kwargs['operations'][0].path_or_fileobj
        self.assertEqual(json.loads(payload)['updated_by'], 'hf:new-admin-id')
        self.assertEqual(json.loads(payload)['schema_version'], 2)

    def test_collaborator_roles_cannot_publish_even_with_a_forged_admin_audit_field(self):
        for role in ('read', 'contributor', 'write', 'no_access', None):
            self.hub.api.whoami.return_value['orgs'][0]['roleInOrg'] = role
            self.assertFalse(is_workspace_admin(self.hub), role)
            with self.assertRaises(PermissionError):
                publish_shared_settings(self.hub, shared_values())
        self.hub.api.create_commit.assert_not_called()

    def test_private_control_repo_access_failure_never_falls_back_to_data_repo(self):
        self.hub.api.repo_info.side_effect = PermissionError('No access')
        with self.assertRaises(PermissionError):
            fetch_shared_settings(self.hub)
        self.assertEqual(self.hub.api.repo_info.call_args.kwargs['repo_id'], CONTROL_REPO)
        self.session.get.assert_not_called()

    def test_publisher_without_settings_write_scope_cannot_create_a_commit(self):
        self.hub.api.auth_check.side_effect = PermissionError('Read-only token')
        with self.assertRaises(PermissionError):
            publish_shared_settings(self.hub, shared_values())
        self.hub.api.create_commit.assert_not_called()

    def test_legacy_document_is_not_accepted_as_a_private_store_migration(self):
        self.document['schema_version'] = 1
        self.path.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(ValueError, 'unsupported format'):
            fetch_shared_settings(self.hub)

    def test_account_change_during_refresh_cannot_apply_another_accounts_result(self):
        def changed(url, **kwargs):
            self.hub.token = 'replacement-token'
            return httpx.Response(200, content=self.path.read_bytes(), request=httpx.Request('GET', url))
        self.session.get.side_effect = changed
        with self.assertRaises(PermissionError):
            fetch_shared_settings(self.hub)

    def test_refresh_preserves_profile_and_analysis_preferences(self):
        self.preferences.set('collaboration', 'collaborator_id', 'My laboratory')
        settings = shared_values(); settings['colab_notebook_url'] = 'https://colab.research.google.com/drive/shared'
        before = copy.deepcopy(self.preferences.get_all())
        self.assertFalse(apply_shared_settings(dict(settings=settings)))
        self.assertEqual(self.preferences.get('collaboration', 'collaborator_id'), 'My laboratory')
        self.assertEqual(self.preferences.get_all()['analyzer'], before['analyzer'])
        self.assertEqual(self.preferences.get('collaboration', 'colab_notebook_url'), settings['colab_notebook_url'])

    def test_live_repository_switch_is_saved_for_restart_without_rerouting_edits(self):
        settings = shared_values(); settings['hf_dataset_repo'] = 'tempo-bio/next-dataset'
        previous = self.preferences.get('collaboration', 'hf_dataset_repo')
        self.assertTrue(apply_shared_settings(dict(settings=settings), live=True))
        self.assertEqual(self.preferences.get('collaboration', 'hf_dataset_repo'), previous)
        self.assertEqual(self.preferences.save_to_disk.call_args.args[0]['collaboration']['hf_dataset_repo']['value'], settings['hf_dataset_repo'])

    def test_later_profile_save_preserves_repository_update_waiting_for_restart(self):
        settings = shared_values(); settings['hf_dataset_repo'] = 'tempo-bio/next-dataset'
        apply_shared_settings(dict(settings=settings), live=True)
        self.preferences.set('collaboration', 'collaborator_id', 'New laboratory name')
        target = self.root/'preferences.json'
        with patch('tempo_config.CONFIG_DIR', str(self.root)), patch('tempo_config.CONFIG_PATH', str(target)):
            self.assertTrue(self.preferences.save())
        saved = json.loads(target.read_text())
        self.assertEqual(saved['collaboration']['hf_dataset_repo']['value'], 'tempo-bio/next-dataset')
        self.assertEqual(saved['collaboration']['collaborator_id']['value'], 'New laboratory name')
        self.assertEqual(self.preferences.get('collaboration', 'hf_dataset_repo'), shared_values()['hf_dataset_repo'])

    def test_reverted_remote_repository_change_clears_the_pending_restart_cache(self):
        settings = shared_values(); settings['hf_dataset_repo'] = 'tempo-bio/next-dataset'
        self.assertTrue(apply_shared_settings(dict(settings=settings), live=True))
        self.assertFalse(apply_shared_settings(dict(settings=shared_values()), live=True))
        self.assertIsNone(self.preferences._pending_workspace)
        saved = self.preferences.save_to_disk.call_args.args[0]
        self.assertEqual(saved['collaboration']['hf_dataset_repo']['value'], shared_values()['hf_dataset_repo'])

    def test_bad_or_unsavable_settings_leave_previous_values_intact(self):
        before = copy.deepcopy(self.preferences.get_all())
        for key, value in [('hf_dataset_repo', '../escape'), ('colab_notebook_url', 'https://example.com/')]:
            settings = shared_values(); settings[key] = value
            with self.assertRaises(ValueError):apply_shared_settings(dict(settings=settings))
        self.preferences.save_to_disk.return_value = False
        settings = shared_values(); settings['colab_notebook_url'] = 'https://colab.research.google.com/drive/new'
        with self.assertRaises(OSError):apply_shared_settings(dict(settings=settings))
        self.assertEqual(self.preferences.get_all(), before)

    def test_dataset_cache_paths_are_separate_and_legacy_path_is_preserved(self):
        old = get_collaboration_dir('tempo-bio/cardiomyocyte-data')
        other = get_collaboration_dir('tempo-bio/next-dataset')
        self.assertNotEqual(old, other)
        self.assertEqual(Path(old).parts[-2:], ('datasets', 'universal_images'))
        self.assertEqual(other, get_collaboration_dir('tempo-bio/next-dataset'))

    def test_training_bundle_does_not_embed_desktop_administrator_configuration(self):
        from tempo_cloud import archive_sources
        with zipfile.ZipFile(io.BytesIO(archive_sources(Path(__file__).resolve().parents[1]))) as bundle:
            self.assertNotIn('tempo_shared_settings.py', bundle.namelist())

    def test_public_dataset_cannot_be_published(self):
        self.hub.api.repo_info.return_value.private = False
        with self.assertRaisesRegex(ValueError, 'private'):
            publish_shared_settings(self.hub, shared_values())
        self.hub.api.create_commit.assert_not_called()

    def test_publisher_checks_concurrent_settings_and_only_writes_control_file(self):
        settings = shared_values(); settings['colab_notebook_url'] = 'https://colab.research.google.com/drive/new'
        previous = shared_values(); previous['colab_notebook_url'] = 'https://colab.research.google.com/drive/stale'
        with self.assertRaisesRegex(ValueError, 'changed remotely'):
            publish_shared_settings(self.hub, settings, previous=previous)
        self.hub.api.create_commit.assert_not_called()
        publish_shared_settings(self.hub, settings, previous=shared_values())
        args = self.hub.api.create_commit.call_args.kwargs
        self.assertEqual(args['repo_id'], CONTROL_REPO)
        self.assertEqual([op.path_in_repo for op in args['operations']], [CONTROL_FILE])
        self.assertEqual(args['parent_commit'], 'pinned-revision')


class WorkspaceInteractionTests(unittest.TestCase):
    def test_notebook_shortcut_scrolls_to_the_editor_and_preserves_unsaved_edits(self):
        from PyQt6.QtWidgets import QTabWidget, QWidget
        view = gui.SettingsTab()
        tabs = QTabWidget(); tabs.addTab(QWidget(), 'Maintainer'); tabs.addTab(view, 'Settings')
        self.addCleanup(close_fixture, tabs)
        tabs.resize(900, 600); tabs.show(); APP.processEvents()
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(view, 'refresh_shared_settings') as refresh:
            view._set_shared_access(True, 'fixture-token')
            field = view.widgets[('collaboration', 'colab_notebook_url')]
            field.setText('https://colab.research.google.com/drive/unsaved')
            host = SimpleNamespace(tabs=tabs, tab_settings=view)
            gui.TempoDashboard.edit_notebook_url(host); APP.processEvents()
        self.assertIs(tabs.currentWidget(), view)
        self.assertTrue(field.hasFocus()); self.assertFalse(field.isReadOnly())
        self.assertEqual(field.selectedText(), 'https://colab.research.google.com/drive/unsaved')
        self.assertTrue(view.settings_scroll.viewport().rect().contains(field.mapTo(view.settings_scroll.viewport(), field.rect().center())))
        refresh.assert_not_called()

    def test_notebook_shortcut_checks_permissions_before_unlocking_editor(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch('tempo_shared_settings.is_workspace_admin', return_value=False), \
             patch('tempo_shared_settings.fetch_shared_settings', return_value=dict(settings=shared_values(), revision='pinned')), \
             patch('tempo_shared_settings.apply_shared_settings', return_value=False), \
             patch.object(gui, 'run_background', side_effect=lambda work, done: done(work(), None)):
            view.focus_notebook_url()
        self.assertTrue(view.widgets[('collaboration', 'colab_notebook_url')].isReadOnly())
        self.assertIn('Only the administrator', view.shared_settings_status.text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in (('tempo_config.CONFIG_DIR', str(self.root)),
                              ('tempo_config.CONFIG_PATH', str(self.root/'preferences.json')),
                              ('tempo_gui.tempo_preferences._preferences', copy.deepcopy(DEFAULT_PREFERENCES)),
                              ('tempo_gui.tempo_preferences._pending_workspace', None)):
            replacement = patch(target, value); replacement.start(); self.addCleanup(replacement.stop)

    def analyzer(self):
        view = gui.AnalyzerTab(); self.addCleanup(close_fixture, view)
        view.load_csv(str(write_historical_dataset(self.root)))
        view.resize(1100, 950); view.show(); APP.processEvents()
        return view

    def test_full_well_option_restores_summary_after_cell_selection_in_each_view(self):
        view = self.analyzer()
        for field in (0, 1, 3):
            view.field_selector.setCurrentIndex(field)
            selection = view.field_selector.currentData()
            expected = view.lbl_amp.text()
            view.cell_selector.setCurrentIndex(2)
            self.assertNotIn('Well Averages', view.lbl_target.text())
            view.cell_selector.setCurrentIndex(0)
            self.assertEqual(view.cell_selector.currentText(), 'Full Well')
            self.assertIn('Well Averages', view.lbl_target.text())
            self.assertEqual(view.lbl_amp.text(), expected)
            self.assertEqual(view.field_selector.currentData(), selection)
            self.assertTrue(view.mini_plot.isHidden())

    def test_real_pointer_hover_and_keyboard_focus_update_the_persistent_help_panel(self):
        view = self.analyzer()
        label = view.form_core.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()
        QTest.mouseMove(view, QPoint(2, 2)); QTest.mouseMove(label, label.rect().center()); QTest.qWait(200)
        self.assertIn('Amplitude', view.statistics_help.topic.text())
        self.assertIn('arbitrary units', view.statistics_help.details.text())
        self.assertFalse(label.toolTip())
        QTest.mouseMove(view, QPoint(2, 2)); QTest.qWait(180)
        self.assertIn('arbitrary units', view.statistics_help.details.text())
        rate = view.form_vel.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()
        rate.setFocus(); APP.processEvents()
        self.assertIn('per millisecond', view.statistics_help.details.text())

    def test_shared_fields_are_readonly_and_personal_save_ignores_programmatic_tampering(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        for key in SHARED_KEYS:self.assertTrue(view.widgets[('collaboration', key)].isReadOnly())
        self.assertFalse(view.widgets[('collaboration', 'collaborator_id')].isReadOnly())
        view.widgets[('collaboration', 'hf_dataset_repo')].setText('someone/other-dataset')
        previous = gui.tempo_preferences.get('collaboration', 'hf_dataset_repo')
        with patch.object(gui.tempo_preferences, 'save_to_disk', return_value=True) as save:
            view.save_preferences()
        self.assertEqual(save.call_args.args[0]['collaboration']['hf_dataset_repo']['value'], previous)
        with patch('tempo_shared_settings.publish_shared_settings') as publish:
            view.update_shared_settings()
        publish.assert_not_called()
        self.assertTrue(view.btn_update_shared.isHidden())

    def test_reservation_names_are_centered_with_ids_only_in_hover_and_identity_data(self):
        import maintainer_tab
        with patch.object(maintainer_tab, 'run_background'):
            view = maintainer_tab.MaintainerTab(Mock(universal_annotations={}))
        self.addCleanup(close_fixture, view); view.timer.stop()
        with patch.object(view, 'refresh_local'):
            view.apply_remote_images(dict(collaborators={'hf:other':dict(profile_name='Cardiac Imaging Lab')}, images={
                'field.png':dict(status='locked', claimed_by='hf:other')}))
        owner = view.leases.item(0, 1)
        self.assertEqual(owner.text(), 'Cardiac Imaging Lab')
        self.assertIn('hf:other', owner.toolTip())
        self.assertEqual(owner.data(Qt.ItemDataRole.UserRole), 'hf:other')
        for column in range(5):self.assertTrue(view.leases.item(0, column).textAlignment() & Qt.AlignmentFlag.AlignHCenter)
        view.lease_search.setText('hf:other')
        self.assertFalse(view.leases.isRowHidden(0))

    def test_legacy_unnamed_account_is_never_shown_as_a_raw_id(self):
        self.assertEqual(TempoHub('fixture-token').display_collaborator('hf:old-id'), 'Unnamed collaborator')

    def test_malformed_or_secret_shaped_remote_profiles_are_rejected(self):
        for profiles in ([], {'hf:actor': []}, {'hf:actor': dict(profile_name='hf_notAValidDisplayName012345')}):
            with self.assertRaises(ValueError):validate_lock_manifest(dict(images={}, collaborators=profiles))

    def test_shared_refresh_does_not_apply_unsaved_analysis_preferences_or_change_selection(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        pending = []
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'run_background', side_effect=lambda work, done: pending.append(done)):
            personal = view.widgets[('analyzer', 'default_metric')]
            personal.setCurrentIndex((personal.currentIndex() + 1) % personal.count())
            selected = personal.currentData(); signal = Mock(); view.settings_saved.connect(signal)
            view.refresh_shared_settings(); pending[0]((False, None, None), None)
            self.assertEqual(personal.currentData(), selected)
            signal.assert_not_called()

    def test_late_admin_check_cannot_unlock_fields_after_account_switch(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        pending = []
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'run_background', side_effect=lambda work, done: pending.append(done)):
            view.refresh_shared_settings()
            gui.tempo_hub.token = 'different-account'
            pending[0]((True, None, None), None)
            for key in SHARED_KEYS:self.assertTrue(view.widgets[('collaboration', key)].isReadOnly())

    def test_successful_remote_publication_with_failed_local_save_is_reported_honestly(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'confirm_action', return_value=True), \
             patch.object(gui, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch('tempo_shared_settings.publish_shared_settings', return_value=dict(settings=dict(shared_values(), colab_notebook_url='https://colab.research.google.com/drive/next'))), \
             patch.object(gui.tempo_preferences, 'save_to_disk', return_value=False):
            view._shared_admin = True; view._shared_admin_token = 'fixture-token'
            view.widgets[('collaboration', 'colab_notebook_url')].setText('https://colab.research.google.com/drive/next')
            view.update_shared_settings()
        self.assertIn('updated', view.shared_settings_error.summary.text())
        self.assertIn('could not be saved', view.shared_settings_error.summary.text())
        self.assertTrue(view.btn_update_shared.isEnabled())

    def test_personal_save_never_publishes_even_with_unsaved_shared_edits_or_first_publication(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch('tempo_shared_settings.publish_shared_settings') as publish, \
             patch.object(gui, 'confirm_action') as confirm:
            view._set_shared_access(True, 'fixture-token'); view._shared_published = False
            original = gui.tempo_preferences.get('collaboration', 'colab_notebook_url')
            view.widgets[('collaboration', 'colab_notebook_url')].setText('https://colab.research.google.com/drive/unsaved')
            fps = view.widgets[('capture', 'target_fps')]; fps.setValue(fps.value()+1)
            view.save_preferences()
            saved = json.loads((self.root/'preferences.json').read_text())
            self.assertEqual(saved['capture']['target_fps']['value'], fps.value())
            self.assertEqual(saved['collaboration']['colab_notebook_url']['value'], original)
            self.assertTrue(view._shared_dirty()); self.assertTrue(view.btn_update_shared.isEnabled())
            self.assertEqual(view.lbl_save_status.text(), 'Settings saved')
        publish.assert_not_called(); confirm.assert_not_called()

    def test_shared_update_preserves_unsaved_personal_preferences_and_emits_only_workspace_change(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        target = dict(shared_values(), colab_notebook_url='https://colab.research.google.com/drive/shared')
        personal = view.widgets[('capture', 'target_fps')]; original = personal.value(); personal.setValue(original+1)
        view.widgets[('collaboration', 'collaborator_id')].setText('Unsaved laboratory name')
        saved_signal = Mock(); workspace_signal = Mock()
        view.settings_saved.connect(saved_signal); view.workspace_changed.connect(workspace_signal)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'confirm_action', return_value=True), \
             patch.object(gui, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch('tempo_shared_settings.publish_shared_settings', return_value=dict(settings=target)) as publish:
            view._set_shared_access(True, 'fixture-token')
            view.widgets[('collaboration', 'colab_notebook_url')].setText(target['colab_notebook_url'])
            view.update_shared_settings()
            self.assertFalse(view.btn_update_shared.isEnabled())
        saved = json.loads((self.root/'preferences.json').read_text())
        self.assertEqual(saved['capture']['target_fps']['value'], original)
        shared_profile_default = DEFAULT_PREFERENCES['collaboration']['collaborator_id']['value']
        self.assertEqual(saved['collaboration']['collaborator_id']['value'], shared_profile_default)
        self.assertEqual(gui.tempo_preferences.get('collaboration', 'collaborator_id'), shared_profile_default)
        self.assertEqual(personal.value(), original+1)
        self.assertEqual(view.lbl_save_status.text(), 'Unsaved changes')
        self.assertEqual(saved['collaboration']['colab_notebook_url']['value'], target['colab_notebook_url'])
        publish.assert_called_once(); saved_signal.assert_not_called(); workspace_signal.assert_called_once()

    def test_personal_save_preserves_published_repository_change_and_ignores_later_shared_edits(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        original = gui.tempo_preferences.get('collaboration', 'hf_dataset_repo')
        target = dict(shared_values(), hf_dataset_repo='tempo-bio/next-dataset')
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'confirm_action', return_value=True), \
             patch.object(gui, 'run_task', side_effect=lambda parent, title, action: action()), \
             patch('tempo_shared_settings.publish_shared_settings', return_value=dict(settings=target)) as publish:
            view._set_shared_access(True, 'fixture-token')
            view.widgets[('collaboration', 'hf_dataset_repo')].setText(target['hf_dataset_repo'])
            view.update_shared_settings()
            view.widgets[('collaboration', 'hf_dataset_repo')].setText('tempo-bio/not-updated')
            fps = view.widgets[('capture', 'target_fps')]; fps.setValue(fps.value()+1)
            view.save_preferences()
        saved = json.loads((self.root/'preferences.json').read_text())
        self.assertEqual(saved['collaboration']['hf_dataset_repo']['value'], target['hf_dataset_repo'])
        self.assertEqual(gui.tempo_preferences.get('collaboration', 'hf_dataset_repo'), original)
        self.assertEqual(gui.tempo_preferences._pending_workspace, target)
        self.assertTrue(view._shared_dirty()); publish.assert_called_once()

    def test_verified_admin_button_survives_read_failure_and_details_are_redacted(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        token = 'synthetic-credential'
        with patch.object(gui.tempo_hub, 'token', token), \
             patch('tempo_shared_settings.is_workspace_admin', return_value=True), \
             patch('tempo_shared_settings.fetch_shared_settings', side_effect=RuntimeError('certificate failure '+token)), \
             patch.object(gui, 'run_background', side_effect=lambda work, done: done(work(), None)):
            view.refresh_shared_settings()
            self.assertFalse(view.btn_update_shared.isHidden())
            self.assertTrue(view.btn_update_shared.isEnabled())
            self.assertFalse(view.widgets[('collaboration', 'colab_notebook_url')].isReadOnly())
            self.assertIn('certificate failure', view.shared_settings_error.details.toPlainText())
            self.assertNotIn(token, view.shared_settings_error.details.toPlainText())

    def test_account_switch_hides_admin_button_and_blocks_direct_update(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch('tempo_shared_settings.publish_shared_settings') as publish:
            view._set_shared_access(True, 'fixture-token')
            self.assertFalse(view.btn_update_shared.isHidden())
            gui.tempo_hub.token = 'another-account'
            view.update_shared_settings()
            self.assertTrue(view.btn_update_shared.isHidden())
            self.assertTrue(view.widgets[('collaboration', 'colab_notebook_url')].isReadOnly())
        publish.assert_not_called()

    def test_cancelled_shared_update_does_not_write_or_discard_edits(self):
        view = gui.SettingsTab(); self.addCleanup(close_fixture, view)
        with patch.object(gui.tempo_hub, 'token', 'fixture-token'), \
             patch.object(gui, 'confirm_action', return_value=False), \
             patch('tempo_shared_settings.publish_shared_settings') as publish:
            view._set_shared_access(True, 'fixture-token')
            view.widgets[('collaboration', 'colab_notebook_url')].setText('https://colab.research.google.com/drive/unsaved')
            view.update_shared_settings()
            self.assertTrue(view._shared_dirty())
        publish.assert_not_called(); self.assertFalse((self.root/'preferences.json').exists())


class SharedProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.hub = TempoHub('fixture-token')
        self.hub._account_id = 'hf:fixture-account'; self.hub._identity_token = self.hub.token
        self.hub._account_name = 'verified-user'
        self.hub.get_dataset_repo = lambda: 'lab/data'
        self.hub.get_models_repo = lambda: 'lab/models'
        self.hub._prepare_manifest = lambda manifest, revision: (manifest, {})
        self.repo = MemoryRepo(dict(**{'lock_manifest.json':dict(version=1, images={
            'image.png':TempoHub._lease('hf:fixture-account')})}))
        self.hub.api = self.repo
        self.hub._read = lambda name, revision, default=None: copy.deepcopy(self.repo.files.get(name, default))
        self.preferences = object.__new__(TempoConfig)
        self.preferences._preferences = copy.deepcopy(DEFAULT_PREFERENCES)
        for item in (patch('tempo_hub.LOCAL_LOCK_CACHE', str(Path(self.temp.name)/'manifest.json')),
                     patch('tempo_hub.tempo_preferences', self.preferences)):
            item.start(); self.addCleanup(item.stop)

    def test_profile_rename_updates_shared_display_without_changing_ownership(self):
        original = copy.deepcopy(self.repo.files['lock_manifest.json']['images'])
        self.hub._prepare_manifest = Mock(side_effect=AssertionError('A profile rename must not migrate or download research images.'))
        for name in ('Research Laboratory', 'Renamed Laboratory'):
            self.preferences.set('collaboration', 'collaborator_id', name)
            self.hub.publish_collaborator_profile()
            directory = self.repo.files['lock_manifest.json']['collaborators']
            self.assertEqual(directory['hf:fixture-account']['profile_name'], name)
            self.assertEqual(self.repo.files['lock_manifest.json']['images'], original)
            self.assertEqual(self.hub.display_collaborator('hf:fixture-account', profiles=directory), name)

    def test_changed_account_or_destination_stops_a_pending_collaboration_write(self):
        def switched(manifest, revision):
            self.hub.token = 'different-token'
            return {}, True
        with self.assertRaises(PermissionError):self.hub._transaction(switched, 'Fixture write')
        self.assertEqual(self.repo.commits, [])


if __name__ == '__main__':
    unittest.main()
