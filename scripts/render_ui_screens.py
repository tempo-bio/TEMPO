"""Render isolated application reference screens: python -m scripts.render_ui_screens.

Services and hardware are disabled. Research storage and settings use temporary
fixtures. Historical references run their own archived Python source and assets.
"""
import argparse
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch


def render(output, font_size=None):
    output.mkdir(parents=True, exist_ok=True)
    # Qt's default offscreen screen is only 800 physical pixels wide. At 200%
    # scaling it cannot contain these windows or route pointer hover reliably.
    # Keep window sizes unchanged and provide enough virtual desktop space.
    screen_config = output / 'offscreen-screen.json'
    screen_config.write_text(json.dumps({'screens': [dict(name='TEMPO review', width=4096,
                                                          height=2304)]}), encoding='utf-8')
    os.environ['QT_QPA_PLATFORM'] = 'offscreen:configfile=' + os.path.relpath(screen_config)
    from PyQt6.QtCore import QTimer, QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication
    application = QApplication.instance() or QApplication([])
    application.setStyle('Fusion')
    from tempo_ui import prepare_offscreen_fonts
    font_evidence = prepare_offscreen_fonts(application)
    if font_size:
        font = application.font(); font.setPointSize(font_size); application.setFont(font)
    with patch('platform.machine', return_value='x86_64'):
        import tempo_gui as gui
    import maintainer_tab
    import annotation_review_dialog
    from tempo_config import DEFAULT_PREFERENCES
    output.mkdir(parents=True, exist_ok=True)
    (output/'fonts.json').write_text(json.dumps(font_evidence, indent=2)+'\n', encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='tempo-screen-fixture-') as directory, ExitStack() as patches:
        root = Path(directory).resolve()
        manifest = {'images': {}}
        patches.enter_context(patch.object(gui.tempo_preferences, '_preferences', copy.deepcopy(DEFAULT_PREFERENCES)))
        patches.enter_context(patch.object(gui.tempo_backend, 'UNIVERSAL_DATASET_DIR', str(root)))
        patches.enter_context(patch.object(gui.tempo_backend, 'QUEUE_FILE', str(root / 'queue.json')))
        patches.enter_context(patch.object(gui.tempo_hub, 'get_collaborator_id', return_value='Screen Fixture'))
        patches.enter_context(patch.object(gui.tempo_hub, 'cached_lock_manifest', return_value=manifest))
        patches.enter_context(patch.object(gui.tempo_hub, 'fetch_lock_manifest', return_value=manifest))
        patches.enter_context(patch.object(gui.tempo_hub, 'is_maintainer', return_value=False))
        patches.enter_context(patch.object(gui, 'run_background', return_value=None))
        patches.enter_context(patch.object(maintainer_tab, 'run_background', return_value=None))
        patches.enter_context(patch.object(annotation_review_dialog, 'run_background', return_value=None))
        patches.enter_context(patch.object(QTimer, 'singleShot', return_value=None))
        window = gui.TempoDashboard()
        window.tabs.setTabVisible(window.tabs.indexOf(window.tab_maintainer), True)
        for timer in window.findChildren(QTimer):
            timer.stop()
        if hasattr(window.tab_maintainer, 'pages'):
            if hasattr(window.tab_maintainer, 'access_ready'):
                window.tab_maintainer.access_ready(True, None)
            window.tab_maintainer.pages.setEnabled(True)
            if hasattr(window.tab_maintainer, 'retry_access'):
                window.tab_maintainer.retry_access.hide()
            for timer in window.findChildren(QTimer):
                timer.stop()
        sizes = []

        def capture(name, widget, size, reveal=None):
            window.tabs.setCurrentWidget(widget)
            window.resize(*size)
            window.show()
            for _ in range(8):
                application.processEvents()
            if reveal is not None:
                scroll, target = reveal
                scroll.ensureWidgetVisible(target, 0, 30)
                for _ in range(8):
                    application.processEvents()
            filename = f'{name}-{size[0]}x{size[1]}.png'
            widget.grab().save(str(output / filename))
            sizes.append({'image': filename, 'requested_window': size,
                          'actual_window': [window.width(), window.height()],
                          'widget_size': [widget.width(), widget.height()]})

        def capture_models(state, size):
            window.tabs.setCurrentWidget(window.tab_annotator)
            window.resize(*size)
            application.processEvents()
            region = window.tab_annotator.auto_group
            filename = f'annotation-models-{state}-{size[0]}x{size[1]}.png'
            region.grab().save(str(output / filename))
            sizes.append({'image': filename, 'requested_window': size,
                          'actual_window': [window.width(), window.height()],
                          'widget_size': [region.width(), region.height()], 'region': 'Detect Cells'})

        for size in ((1100, 760), (960, 620)):
            for name, widget in (
                ('acquisition', window.tab_capture), ('annotation', window.tab_annotator),
                ('analysis-empty', window.tab_analyzer), ('processing', window.tab_cruncher),
                ('settings', window.tab_settings), ('maintainer', window.tab_maintainer),
            ):
                capture(name, widget, size)
            if hasattr(window.tab_analyzer, 'field_selector'):
                capture_models('empty-disabled', size)
            if hasattr(window.tab_maintainer, 'pages'):
                for index in range(window.tab_maintainer.pages.count()):
                    window.tab_maintainer.pages.setCurrentIndex(index)
                    capture(f'maintainer-page-{index}', window.tab_maintainer, size)

        if hasattr(window.tab_analyzer, 'field_selector'):
            import numpy as np
            import pandas as pd
            from tests.presentation_fixtures import write_historical_dataset
            window.tab_analyzer.metric_combo.setCurrentIndex(
                window.tab_analyzer.metric_combo.findData('Peak_Amplitude_AreaNorm_au'))
            window.tab_analyzer.load_csv(str(write_historical_dataset(root)))
            for size in ((1100, 760), (960, 620), (1500, 950)):
                capture('analysis-historical', window.tab_analyzer, size)
            window.tab_analyzer.field_selector.setCurrentIndex(1)
            window.tab_analyzer.cell_selector.setCurrentIndex(1)
            capture('analysis-historical-selected', window.tab_analyzer, (960, 620))
            if hasattr(window.tab_analyzer, 'statistics_help'):
                from PyQt6.QtCore import QPoint
                from PyQt6.QtTest import QTest
                from PyQt6.QtWidgets import QFormLayout
                analyzer = window.tab_analyzer
                for tab, form, name in ((0, analyzer.form_core, 'amplitude'), (1, analyzer.form_core_std, 'variability')):
                    analyzer.stats_tabs.setCurrentIndex(tab)
                    label = form.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()
                    analyzer.inspector_scroll.ensureWidgetVisible(label)
                    application.processEvents()
                    screen = window.screen().availableGeometry()
                    target = label.mapToGlobal(label.rect().center())
                    assert screen.contains(target), f'Hover target {target} is outside virtual screen {screen}'
                    # Use QtTest's window mouse path: the widget overload uses
                    # QCursor, whose offscreen plugin mixes physical/logical
                    # coordinates at non-unit scale. These are mouse moves,
                    # with normal child hit-testing, never injected help events.
                    handle = window.windowHandle()
                    QTest.mouseMove(handle, analyzer.mapTo(window, QPoint(2, 2))); QTest.qWait(30)
                    QTest.mouseMove(handle, label.mapTo(window, label.rect().center())); QTest.qWait(180)
                    assert 'arbitrary units' in analyzer.statistics_help.details.text(), (
                        f'Help did not update for {name}: target='
                        f'{label.mapToGlobal(label.rect().center())}, screen={screen}')
                    assert ('Standard deviation' in analyzer.statistics_help.topic.text()) == bool(tab), (
                        f'Wrong help topic for {name}: {analyzer.statistics_help.topic.text()!r}; '
                        f'target={label.mapToGlobal(label.rect().center())}; '
                        f'visible={label.visibleRegion().boundingRect()}; screen={screen}')
                    for size in ((960, 620), (1500, 950)):
                        window.resize(*size); application.processEvents()
                        analyzer.inspector_scroll.ensureWidgetVisible(analyzer.statistics_help.details, 0, 0)
                        capture('analysis-help-'+name, analyzer, size)
                analyzer.stats_tabs.setCurrentIndex(0)
                analyzer.cell_selector.setCurrentIndex(0)
                capture('analysis-full-well-restored', analyzer, (1100, 760))
                analyzer.inspector_scroll.verticalScrollBar().setValue(0)
                from tempo_hub import TempoHub
                reservations = dict(collaborators={}, images={})
                for index in range(35):
                    owner = f'hf:fixture-{index % 3}'
                    reservations['collaborators'][owner] = dict(profile_name=('Cardiac Imaging Lab', 'Cell Mechanics Group', 'Research Laboratory')[index % 3])
                    reservations['images'][f'field-{index}.png'] = dict(TempoHub._lease(owner), original_filename=f'Field_{index:03}.png')
                window.tab_maintainer.apply_remote_images(reservations)
                window.tab_maintainer.pages.setCurrentWidget(window.tab_maintainer.leases_page)
                for size in ((960, 620), (1500, 950)):
                    capture('reservations-populated', window.tab_maintainer, size)
                window.tab_maintainer.lease_search.setText('Cell Mechanics')
                capture('reservations-filtered', window.tab_maintainer, (960, 620))
                window.tab_maintainer.lease_search.clear()
                from tempo_shared_settings import SHARED_KEYS
                settings = window.tab_settings
                settings.widgets[('collaboration', 'collaborator_id')].setText('Cardiac Imaging Laboratory')
                settings.widgets[('collaboration', 'colab_notebook_url')].setText('https://colab.research.google.com/drive/SyntheticTempoNotebook')
                settings_scroll = settings.findChild(gui.QScrollArea)
                patches.enter_context(patch.object(gui.tempo_hub, 'token', 'screen-fixture-token'))
                for key in SHARED_KEYS:
                    settings._saved_values[('collaboration', key)] = settings.widgets[('collaboration', key)].text()
                settings._shared_published = True
                for admin in (False, True):
                    role = 'admin' if admin else 'collaborator'
                    settings._set_shared_access(admin, gui.tempo_hub.token)
                    settings._set_shared_status('')
                    for size in ((960, 620), (1500, 950)):
                        capture('shared-settings-'+role, settings, size)
                        for position, key in (('notebook', 'colab_notebook_url'), ('repositories', 'hf_models_repo')):
                            capture('shared-settings-'+role+'-'+position, settings, size,
                                    reveal=(settings_scroll, settings.widgets[('collaboration', key)]))
                settings.widgets[('collaboration', 'colab_notebook_url')].setText('https://colab.research.google.com/drive/EditedTempoNotebook')
                for size in ((960, 620), (1500, 950)):
                    capture('shared-settings-admin-unsaved', settings, size,
                            reveal=(settings_scroll, settings.btn_update_shared))
                settings.shared_settings_error.set_error('Could not refresh shared settings. Using saved settings.', RuntimeError('Synthetic connection failure'))
                for size in ((960, 620), (1500, 950)):
                    capture('shared-settings-admin-unavailable', settings, size,
                            reveal=(settings_scroll, settings.shared_settings_error))
            dataset = root / 'results.csv'
            pd.DataFrame({'Recording_ID': ['fixture'] * 3, 'Well_ID': ['A01'] * 3,
                'FOV_ID': ['FOV001', 'FOV002', 'FOV001'], 'Cell_ID': ['1', '1', '2'],
                'Centroid_X': [30, 90, 70], 'Centroid_Y': [40, 60, 80],
                'Peak_Amplitude_au': [5, 9, 7], 'Time_to_Peak_ms': [80, 90, 75],
                'Contraction_Angle_deg': [1, 179, 2],
                'Trace_File': ['cell_1.csv', '', '']}).to_csv(dataset, index=False)
            times = np.arange(0, 2000, 10)
            pd.DataFrame({'Time (ms)': times,
                'Contraction (Raw a.u.)': 5 * np.sin(np.pi * times / 400) ** 2}).to_csv(root / 'cell_1.csv', index=False)
            window.tab_analyzer.load_csv(str(dataset))
            window.tab_analyzer.metric_combo.setCurrentIndex(window.tab_analyzer.metric_combo.findData('Peak_Amplitude_au'))
            window.tab_analyzer.cell_selector.setCurrentIndex(1)
            for size in ((1100, 760), (960, 620)):
                capture('analysis-measured', window.tab_analyzer, size)
                window.tab_analyzer.inspector_scroll.verticalScrollBar().setValue(
                    window.tab_analyzer.inspector_scroll.verticalScrollBar().maximum())
                capture('analysis-trace', window.tab_analyzer, size)
                window.tab_analyzer.inspector_scroll.verticalScrollBar().setValue(0)
            window.tab_analyzer.metric_combo.setCurrentIndex(window.tab_analyzer.metric_combo.findData('Vector_Coherence'))
            capture('analysis-missing-metric', window.tab_analyzer, (960, 620))
            window.tab_analyzer.field_selector.setCurrentIndex(1)
            window.tab_analyzer.cell_selector.setCurrentIndex(1)
            capture('analysis-missing-trace', window.tab_analyzer, (960, 620))
            window.tab_annotator.file_error.set_error('Fixture: annotation loading failed. Retry after checking the file.', OSError('synthetic read error'))
            capture('annotation-error', window.tab_annotator, (960, 620))
            window.tab_maintainer.remote_ready(({'id': 'screen-training'}, {
                'phase': 'training', 'gpu': 'NVIDIA A100', 'updated_at': time.time(),
                'elapsed_seconds': 120, 'models': {
                    'nano_scout': {'phase': 'completed'},
                    'stardist': {'phase': 'initializing'},
                    'cellpose': {'phase': 'training_healthy', 'epoch': 1, 'total': 300},
                }}, None), None)
            window.tab_maintainer.pages.setCurrentWidget(window.tab_maintainer.cloud_scroll)
            capture('cloud-training', window.tab_maintainer, (960, 620))
            from PyQt6.QtGui import QImage
            from tempo_hub import TempoHub
            image = QImage(400, 300, QImage.Format.Format_RGB32)
            image.fill(0x323232)
            image.save(str(root / 'fixture.png'))
            if hasattr(window.tab_analyzer, 'measurement_selector'):
                mixed = pd.read_csv(dataset)
                mixed['Object_Type'] = 'cell'
                groups = mixed.iloc[:2].copy()
                groups['Object_Type'] = 'group'
                groups['Cell_ID'] = ['group_001', 'group_002'][:len(groups)]
                mixed_path = root/'mixed-cell-group.csv'
                pd.concat([mixed, groups], ignore_index=True).to_csv(mixed_path, index=False)
                window.tab_analyzer.load_csv(str(mixed_path))
                for selected, kind in ((0, 'cells'), (1, 'groups')):
                    window.tab_analyzer.measurement_selector.setCurrentIndex(selected)
                    for size in ((960,620), (1500,950)):
                        capture('analysis-'+kind+'-separate', window.tab_analyzer, size)
                window.tab_analyzer.cell_selector.setCurrentIndex(1)
                capture('analysis-group-selected', window.tab_analyzer, (960,620))
                review = annotation_review_dialog.AnnotationReviewDialog(window, verified=True)
                review._loading = False
                review.request_refresh = lambda: None
                review.status.clear(); review.refresh_button.setEnabled(True); review.sub_list.setEnabled(True)
                review.current_submission = {'submission_id':'synthetic-review'}
                for listing, text in ((review.sub_list,'Synthetic submission'),(review.frame_list,'fixture.png')):
                    listing.blockSignals(True); listing.addItem(text); listing.setCurrentRow(0); listing.blockSignals(False)
                review.current_image_filename = 'fixture.png'
                review.image_title.setText('fixture.png')
                review.current_annotations = {'fixture.png': dict(coordinate_space='pixels',width=400,height=300,boxes=[
                    dict(class_id=0,polygon=[[30,60],[130,60],[130,140],[30,140]]),
                    dict(class_id=0,object_type='group',polygon=[[180,100],[350,100],[350,230],[180,230]])])}
                review.reviewed_snapshot = copy.deepcopy(review.current_annotations)
                review.canvas.set_image_and_masks(str(root/'fixture.png'), review.current_annotations['fixture.png'])
                review._displayed_boxes = copy.deepcopy(review.canvas.boxes)
                review._displayed_record = copy.deepcopy(review.current_annotations['fixture.png'])
                review.canvas.polygons[1].setSelected(True)
                review.update_edit_actions()
                for size in ((960,620), (1500,950)):
                    review.resize(*size); review.show()
                    for _ in range(8): application.processEvents()
                    filename = f'review-group-selected-{size[0]}x{size[1]}.png'
                    review.grab().save(str(output/filename))
                    sizes.append(dict(image=filename,requested_window=size,
                                      actual_window=[review.width(),review.height()],
                                      widget_size=[review.width(),review.height()]))
                review.close()
            annotator = window.tab_annotator
            annotator.file_error.clear()
            annotator.mode_toggle.setCurrentIndex(1)
            boxes = [{'class_id': 1, 'polygon': [[60, 70], [160, 70], [140, 170], [70, 160]]}]
            annotator.universal_annotations = {'fixture.png': {'status': 'locked', 'boxes': boxes}}
            manifest['images']['fixture.png'] = TempoHub._lease('Screen Fixture')
            annotator.lock_manifest = manifest
            annotator.refresh_universal_file_list(refresh=False)
            annotator.on_file_selected(annotator.list_files.item(0))
            annotator.update_running_counts()
            assert not annotator._current_file_readonly
            assert annotator.btn_auto_cellpose.isEnabled()
            assert annotator.btn_auto_stardist.isEnabled()
            for size in ((1100, 760), (960, 620)):
                capture('annotation-editable', annotator, size)
                capture_models('editable', size)
            if hasattr(annotator, 'navigator_scroll'):
                annotator.navigator_scroll.ensureWidgetVisible(annotator.btn_guidelines, 0, 0)
                annotator.submission_scroll.ensureWidgetVisible(annotator.btn_publish, 0, 0)
                capture('annotation-actions', annotator, (960, 620))
            manifest['images']['fixture.png']['status'] = 'approved'
            annotator.universal_annotations['fixture.png']['status'] = 'approved'
            annotator.refresh_universal_file_list(refresh=False)
            annotator.on_file_selected(annotator.list_files.item(0))
            assert annotator._current_file_readonly
            for size in ((1100, 760), (960, 620)):
                capture('annotation-approved', annotator, size)
                capture_models('approved-disabled', size)

            from tempo_processing import ProcessingResult, atomic_write
            ready_video = root / 'A01_FOV001.avi'
            ready_video.touch()
            atomic_write(root / 'queue.json', [str(ready_video), str(root / 'missing.avi')])
            window.refresh_processing_queue()
            window.record_processing_result(ProcessingResult(str(ready_video), str(root), 'success',
                ['detection', 'extraction', 'kinematics', 'statistics'], results_csv=str(dataset)), persist=False)
            window.record_processing_result(ProcessingResult(str(root / 'failed.avi'), str(root), 'failure',
                ['detection', 'extraction', 'kinematics'], error='Fixture: statistics could not be saved.'), persist=False)
            window.job_status.setText('1 recording completed; 1 failed and remains queued.')
            for size in ((1100, 760), (960, 620)):
                capture('processing-outcomes', window.tab_cruncher, size)
            window.lock_buttons()
            window.job_status.setText('Processing A01_FOV001.avi…')
            capture('processing-running', window.tab_cruncher, (960, 620))
            window.unlock_buttons()
            queued = [str(root/f'queue_{index}.avi') for index in range(100)]
            atomic_write(root/'queue.json', queued)
            window.refresh_processing_queue()
            window.job_status.setText('Queued recordings need their file locations resolved.')
            for size in ((1100, 950), (960, 620)):
                capture('processing-long-queue', window.tab_cruncher, size)

            unknown = root / 'trace-only.csv'
            pd.DataFrame({'Well_ID': ['A01'], 'Cell_ID': ['1'], 'Total_Beats_Analyzed': [0],
                          'Peak_Amplitude_au': [np.nan], 'Trace_File': ['cell_1.csv']}).to_csv(unknown, index=False)
            window.tab_analyzer.load_csv(str(unknown))
            window.tab_analyzer.cell_selector.setCurrentIndex(1)
            capture('analysis-unknown-metadata', window.tab_analyzer, (960, 620))
        guidelines = gui.GuidelinesDialog(window)
        for size in ((720,620), (960,620), (1500,950)):
            guidelines.resize(*size); guidelines.show()
            for _ in range(8): application.processEvents()
            scroll = guidelines.findChild(gui.QScrollArea)
            for section in guidelines.findChildren(gui.QGroupBox):
                scroll.ensureWidgetVisible(section, 0, 0)
                for _ in range(8): application.processEvents()
                title = section.title().split('.')[0].lower().replace(' ', '-')
                filename = f'guidelines-{title}-{size[0]}x{size[1]}.png'
                guidelines.grab().save(str(output/filename))
                sizes.append(dict(image=filename, requested_window=size,
                                  actual_window=[guidelines.width(),guidelines.height()],
                                  widget_size=[guidelines.width(),guidelines.height()]))
            for index, example in enumerate(guidelines.findChildren(gui.GuidelineIllustration)):
                scroll.ensureWidgetVisible(example, 0, 0)
                for _ in range(8): application.processEvents()
                filename = f'guidelines-example-{index+1}-{size[0]}x{size[1]}.png'
                guidelines.grab().save(str(output/filename))
                sizes.append(dict(image=filename, requested_window=size,
                                  actual_window=[guidelines.width(),guidelines.height()],
                                  widget_size=[guidelines.width(),guidelines.height()]))
        guidelines.close()
        (output / 'geometry.json').write_text(json.dumps(sizes, indent=2))
        window.close()
        quit_timer = QTimer(application)
        quit_timer.setSingleShot(True)
        quit_timer.timeout.connect(application.quit)
        quit_timer.start(20)
        application.exec()
        import tempo_tasks
        if hasattr(tempo_tasks, 'dispose_application'):
            tempo_tasks.dispose_closed_views(window)
            tempo_tasks.dispose_application(application)
        else:
            # Historical source references retain their own lifecycle API.
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        mismatches = [item for item in sizes if list(item['requested_window']) != item['actual_window']]
        if mismatches:
            raise RuntimeError('Fixture windows could not fit requested sizes: '+json.dumps(mismatches))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--revision')
    parser.add_argument('--font-size', type=int)
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.revision:
        render(output, args.font_size)
        return
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='tempo-reference-source-') as directory:
        root = Path(directory).resolve()
        names = subprocess.check_output(['git', 'ls-tree', '--name-only', args.revision], cwd=repository, text=True).splitlines()
        sources = [name for name in names if name.endswith('.py')] + ['assets']
        archive_path = root / 'source.tar'
        with archive_path.open('wb') as stream:
            subprocess.run(['git', 'archive', args.revision, *sources], cwd=repository, stdout=stream, check=True)
        with tarfile.open(archive_path) as archive:
            for member in archive.getmembers():
                if not (root / member.name).resolve().is_relative_to(root) or member.issym() or member.islnk():
                    raise ValueError('Unsafe historical archive entry.')
            archive.extractall(root)
        (root / 'scripts').mkdir()
        shutil.copy2(__file__, root / 'scripts' / Path(__file__).name)
        command = [sys.executable, '-m', 'scripts.render_ui_screens', '--output', str(output)]
        if sys.platform == 'darwin':
            command = ['arch', '-' + platform.machine(), *command]
        subprocess.run(command, cwd=root, check=True, timeout=30)


if __name__ == '__main__':
    main()
