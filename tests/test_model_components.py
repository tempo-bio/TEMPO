"""Scout preprocessing, model resolution and segmentation-loss gradient checks."""
import unittest
import tempfile
from unittest.mock import patch
import numpy as np
import torch

from yolo_inference_helper import Tier1ScoutEngine, resolve_model_path
from train_yolo import BoundaryAwareSegmentationLoss


class ModelComponentTests(unittest.TestCase):

    def test_tier1_scout_engine_preprocessing_and_resolution(self):
        """Verify aspect-ratio preserving downsampling for FLIR 1440x1080 sensor."""
        with patch('yolo_inference_helper.resolve_model_path', return_value='/mock/scout.pt'):
            engine = Tier1ScoutEngine()

        # Simulate FLIR BFS-U3-16S2M-CS native frame (1080 H, 1440 W)
        flir_frame = np.random.randint(0, 255, (1080, 1440, 3), dtype=np.uint8)

        padded, scale, (left, top) = engine.preprocess_frame(flir_frame, target_size=(640, 480))
        assert padded.shape == (480, 640, 3)
        assert scale > 0

    def test_model_path_resolution(self):
        """Verify model path resolution returns expected base paths or fallbacks."""
        from pathlib import Path
        from yolo_inference_helper import MODEL_REGISTRY
        import yolo_inference_helper as helper
        from tempo_hub import tempo_hub
        with tempfile.TemporaryDirectory() as folder:
            for name in set(MODEL_REGISTRY.values()):
                (Path(folder) / name).write_bytes(b'mock')
            with patch('tempo_config.get_base_dir', return_value=folder), \
                 patch('tempo_model_catalog.installed_model_dir', return_value=Path(folder)/'installed'):
                for alias in ('scout', 'stardist', 'cellpose'):
                    assert Path(resolve_model_path(alias)).name == MODEL_REGISTRY[alias]
                (Path(folder) / MODEL_REGISTRY['stardist']).unlink()
                with patch.object(tempo_hub, 'download_champion_weights', return_value=(False, 'offline')):
                    try:
                        resolve_model_path('stardist')
                        raise AssertionError('Missing StarDist must not resolve to YOLO')
                    except FileNotFoundError:
                        pass

    def test_tier1_scout_mock_inference(self):
        """Verify scout engine viability evaluation schema."""
        with patch('yolo_inference_helper.resolve_model_path', return_value='/mock/scout.pt'):
            engine = Tier1ScoutEngine()
        test_img = np.zeros((1080, 1440, 3), dtype=np.uint8)

        with patch('yolo_inference_helper.run_inference', return_value={'success': True, 'boxes': [{'class_id': 0, 'conf': .9}]}):
            res = engine.evaluate_viability(test_img, live_threshold=3)
        assert "is_viable" in res
        assert "live_count" in res
        assert "dead_count" in res
        assert isinstance(res["live_count"], int)

    def test_registered_basename_prefers_installation_while_explicit_path_is_preserved(self):
        from contextlib import chdir
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); installed = root/'installed'; installed.mkdir()
            filename = 'yolo26n-scout.pt'
            (root/filename).write_bytes(b'old local weights')
            (installed/filename).write_bytes(b'new installed weights')
            with chdir(root), patch('tempo_model_catalog.installed_model_dir', return_value=installed):
                self.assertEqual(resolve_model_path(filename), str(installed/filename))
                self.assertEqual(Path(resolve_model_path('./'+filename)).resolve(), (root/filename).resolve())

    def test_boundary_aware_segmentation_loss_forward_and_backward(self):
        """Verify boundary-aware morphological single_mask_loss computes finite scalar and flows gradients."""
        device = "cpu"
        N = 2
        H, W = 64, 64
        pred = torch.randn(N, 32, requires_grad=True, device=device)
        proto = torch.randn(32, H, W, requires_grad=True, device=device)
        gt_mask = torch.zeros(N, H, W, device=device)
        gt_mask[:, 20:44, 20:44] = 1.0  # Square cell
        xyxy = torch.tensor([[15.0, 15.0, 48.0, 48.0], [15.0, 15.0, 48.0, 48.0]], device=device)
        area = torch.tensor([[24.0 * 24.0], [24.0 * 24.0]], device=device)

        loss = BoundaryAwareSegmentationLoss.single_mask_loss(gt_mask, pred, proto, xyxy, area)
        assert torch.isfinite(loss), "Loss must be finite"
        assert loss.item() > 0, "Loss must be positive"

        loss.backward()
        assert pred.grad is not None
        assert proto.grad is not None


if __name__ == "__main__":
    unittest.main()
