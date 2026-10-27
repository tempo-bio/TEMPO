"""Personal storage migration preserves legacy data and running-job destinations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import tempo_backend as backend
from tempo_config import get_data_dir
from tempo_processing import ProcessingResult, record_result, read_queue, read_result_history


class CaptureStorageTests(unittest.TestCase):
    def test_default_and_selected_data_folder_do_not_create_directories(self):
        with tempfile.TemporaryDirectory() as directory, patch('os.path.expanduser', side_effect=lambda value: directory+value[1:] if value.startswith('~') else value):
            self.assertEqual(get_data_dir(''),str(Path(directory)/'Documents'/'TEMPO'))
            self.assertEqual(get_data_dir(str(Path(directory)/'chosen')),str(Path(directory)/'chosen'))
            self.assertFalse(list(Path(directory).iterdir()))

    def test_migration_copies_paths_and_history_once_without_moving_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=root/'application';old.mkdir();config=root/'personal';config.mkdir()
            source=old/'Assays'/'Raw'/'recording.tif';source.parent.mkdir(parents=True);source.write_bytes(b'research data')
            queue=old/'pending_queue.json';queue.write_text(json.dumps([str(source)]))
            result=ProcessingResult(str(source),str(old/'Assays'/'Processed'/'result'),outcome='success')
            record_result(str(queue)+'.results.json',result)
            target=config/'processing'/'pending_queue.json'
            with patch.object(backend,'BASE_DIR',str(old)),patch.object(backend,'QUEUE_FILE',str(target)),patch.object(backend,'get_config_dir',return_value=str(config)):
                backend.initialize_processing_storage()
                self.assertEqual(read_queue(target),[str(source)])
                self.assertEqual(read_result_history(str(target)+'.results.json')[0].output_dir,result.output_dir)
                target.write_text('[]')
                backend.initialize_processing_storage()
                self.assertEqual(read_queue(target),[],'A completed queue must not be repopulated on next launch.')
            self.assertEqual(source.read_bytes(),b'research data')
            self.assertEqual(read_queue(queue),[str(source)])

    def test_invalid_legacy_queue_is_preserved_without_publishing_a_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);queue=root/'pending_queue.json';queue.write_text('{broken')
            target=root/'personal'/'processing'/'pending_queue.json'
            with patch.object(backend,'BASE_DIR',str(root)),patch.object(backend,'QUEUE_FILE',str(target)),patch.object(backend,'get_config_dir',return_value=str(root/'personal')):
                with self.assertRaises(ValueError):backend.initialize_processing_storage()
            self.assertEqual(queue.read_text(),'{broken');self.assertFalse(target.exists())

    def test_retention_failure_leaves_source_queued_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'capture.tif';source.write_bytes(b'recoverable source')
            queue=root/'queue.json';queue.write_text(json.dumps([str(source)]))
            result=ProcessingResult(str(source),str(root/'results'),outcome='success')
            with patch.object(backend,'QUEUE_FILE',str(queue)),patch.object(backend,'process_assay_result',return_value=result),patch.object(backend,'finalize_processing',side_effect=OSError('Retention failed')):
                summary=backend.process_entire_queue()
            self.assertEqual(summary['failed'],1)
            self.assertEqual(read_queue(queue),[str(source)])
            self.assertEqual(source.read_bytes(),b'recoverable source')
