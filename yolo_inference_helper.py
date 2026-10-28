import sys
import json
import os
import time
import tempfile
import zipfile
if __name__ == '__main__':
    from tempo_runtime import prepare_windows_runtime
    prepare_windows_runtime()
import cv2
import platform
import subprocess
import numpy as np
from tempo_regions import annotation_class, region_counts, scout_qualifies

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_REGISTRY = {
    "nano": "yolo26n-scout.pt",
    "scout": "yolo26n-scout.pt",
    "nano_scout": "yolo26n-scout.pt",
    "yolo_scout_nano": "yolo26n-scout.pt",
    "yolo26n": "yolo26n-scout.pt",
    "yolo11n": "yolo11n.pt",
    "stardist": "stardist_nuclei.zip",
    "stardist09": "stardist_nuclei.zip",
    "cellpose": "cellpose4_cardio.pt",
    "cellpose4": "cellpose4_cardio.pt"
}

def resolve_model_path(model_type_or_path=None):
    """Resolves a model alias or filepath to a valid weights file with graceful fallbacks."""
    if not model_type_or_path:
        model_type_or_path = "cellpose"
        
    if model_type_or_path in ('yolo_seg','yolo26s_seg','yolo26s'):
        raise ValueError('YOLO segmentation is retired. Use Cellpose for cell outlines.')
    from tempo_model_catalog import INSTALLABLE_MODEL_FILES, local_model_path
    # Registered basenames prefer the current installation. An explicit path
    # such as ./weights.pt continues to select a user-supplied local model.
    if model_type_or_path not in INSTALLABLE_MODEL_FILES and os.path.exists(model_type_or_path):
        return os.path.abspath(model_type_or_path)

    target_name = MODEL_REGISTRY.get(model_type_or_path.lower(), model_type_or_path)
    primary_path = str(local_model_path(target_name))
    if os.path.exists(primary_path):
        return primary_path

    # Resolve the requested architecture before considering any compatibility alias.
    from tempo_hub import tempo_hub
    success, result = tempo_hub.download_champion_weights(target_name)
    if success:
        return result
    raise FileNotFoundError(f"Requested model {target_name} is unavailable: {result}")


class Tier1ScoutEngine:
    """Load the scout model for cell detection and counting.

    Select an available inference backend; latency depends on the model, image,
    and hardware. Healthy/dead outputs are model classifications.
    """
    def __init__(self, model_path=None, conf=0.15, iou=0.40):
        self.model_path = resolve_model_path(model_path or "nano")
        self.conf = float(conf)
        self.iou = float(iou)
        self.ort_session = None
        self._init_engine()

    def _init_engine(self):
        # Check for ONNX Runtime acceleration on Windows / Linux
        onnx_candidate = os.path.splitext(self.model_path)[0] + ".onnx"
        if os.path.exists(onnx_candidate):
            try:
                import importlib
                ort = importlib.import_module("onnxruntime")
                available_providers = ort.get_available_providers()
                # Windows DirectML first, then CUDA, OpenVINO, CPU
                preferred_order = [
                    'DmlExecutionProvider',      # Windows DirectML (Any GPU: AMD/Intel/NVIDIA)
                    'CUDAExecutionProvider',     # NVIDIA GPU
                    'OpenVINOExecutionProvider', # Intel CPU / NPU
                    'CPUExecutionProvider'
                ]
                providers = [p for p in preferred_order if p in available_providers]
                if not providers: providers = ['CPUExecutionProvider']
                self.ort_session = ort.InferenceSession(onnx_candidate, providers=providers)
                print(f"[SCOUT ENGINE] Hardware-accelerated ONNX Session initialized with: {providers[0]}")
                return
            except Exception as e:
                print(f"[SCOUT ENGINE] ONNX acceleration notice: {e}. Falling back to PyTorch.")

        self.ort_session = None

    def preprocess_frame(self, frame, target_size=(640, 480)):
        """Fast aspect-ratio preserving spatial downsampling optimized for 1440x1080."""
        h, w = frame.shape[:2]
        tw, th = target_size
        scale = min(tw / w, th / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        
        # Letterbox to target size
        top = (th - nh) // 2
        bottom = th - nh - top
        left = (tw - nw) // 2
        right = tw - nw - left
        
        padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        return padded, scale, (left, top)

    def evaluate_viability(self, image_or_path, live_threshold=3):
        """Rapidly screens an image/frame for cell viability (< 5 ms)."""
        t0 = time.perf_counter()
        
        if isinstance(image_or_path, str):
            frame = cv2.imread(image_or_path)
        else:
            frame = image_or_path
            
        if frame is None:
            return {
                "success": False,
                "error": "Invalid image provided to Tier1ScoutEngine",
                "is_viable": False,
                "live_count": 0,
                "dead_count": 0
            }

        # Run inference via Ultralytics or ONNX
        res = run_inference(image_or_path, self.model_path, conf=self.conf, iou=self.iou)
        inference_ms = (time.perf_counter() - t0) * 1000.0
        
        if not res.get("success"):
            return {
                "success": False,
                "error": res.get("error"),
                "is_viable": False,
                "live_count": 0,
                "dead_count": 0,
                "inference_ms": inference_ms
            }
            
        boxes = res.get("boxes", [])
        counts = region_counts(boxes)
        live_count, dead_count = counts['live_count'], counts['dead_count']
        total_cells = live_count + dead_count
        
        viability_ratio = (live_count / max(1, total_cells)) if total_cells > 0 else 0.0
        is_viable = scout_qualifies(counts, live_threshold)
        
        # Uncertainty entropy: measure how close predictions are to decision boundary
        confidences = [b.get("conf", 0.5) for b in boxes]
        uncertainty = float(np.mean([1.0 - abs(c - 0.5) * 2 for c in confidences])) if confidences else 0.0
        
        return {
            "success": True,
            "live_count": live_count,
            "dead_count": dead_count,
            "healthy_groups": counts['healthy_groups'],
            "dead_groups": counts['dead_groups'],
            "total_cells": total_cells,
            "viability_ratio": viability_ratio,
            "is_viable": is_viable,
            "uncertainty_score": uncertainty,
            "inference_ms": inference_ms,
            "boxes": boxes
        }

def mask_polygon(mask, image_shape=None):
    """Return one editable outline without bridging disconnected mask islands.

    TEMPO stores one polygon per instance. Retain its largest external contour
    by area, and remove pixel stair steps within a one-pixel/one-percent area
    tolerance. Never connect separate components into a synthetic outline.
    """
    mask = np.ascontiguousarray(np.asarray(mask) > 0, dtype=np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea).astype(np.float32)
    if image_shape is not None:
        height, width = image_shape[:2]
        contour[:, :, 0] *= width / mask.shape[1]
        contour[:, :, 1] *= height / mask.shape[0]
    area = cv2.contourArea(contour)
    if area <= 0 or len(contour) < 3:
        return None
    simplified = cv2.approxPolyDP(contour, 1.0, True)
    if len(simplified) >= 3 and abs(cv2.contourArea(simplified) - area) <= area * .01:
        contour = simplified
    return contour[:, 0, :].tolist()


def run_inference(image_or_video_path, model_path_or_type=None, conf=0.15, iou=0.40):
    """Executes model inference on an image file or video frame 0 and outputs JSON."""
    temp_img = None
    target_path = image_or_video_path
    model_path = resolve_model_path(model_path_or_type)
    
    if not os.path.exists(model_path):
        err_dict = {"success": False, "error": f"Model weights not found at: {model_path}"}
        print(json.dumps(err_dict))
        return err_dict

    ext = os.path.splitext(image_or_video_path)[1].lower() if isinstance(image_or_video_path, str) else ""
    if ext in ['.avi', '.mp4', '.mov', '.mkv']:
        cap = cv2.VideoCapture(image_or_video_path)
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            err_dict = {"success": False, "error": f"Could not extract frame 0 from: {image_or_video_path}"}
            print(json.dumps(err_dict))
            return err_dict
        fd, temp_img = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        cv2.imwrite(temp_img, frame)
        target_path = temp_img

    try:
        native = os.path.basename(model_path) in ('stardist_nuclei.zip', 'cellpose4_cardio.pt')
        if not native and zipfile.is_zipfile(model_path):
            with zipfile.ZipFile(model_path) as archive:
                native = 'tempo_cellpose.json' in archive.namelist() or 'model/tempo_nuclei.json' in archive.namelist()
        if native:
            from tempo_training import read_image_plane
            image = read_image_plane(target_path) if isinstance(target_path, str) else target_path
            if image is None:
                raise ValueError('Cannot read image.')
            if image.ndim == 3:
                image = image[..., 0] if image.shape[-1] == 1 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if model_path.endswith('.zip'):
                from tempo_training import configure_tensorflow, unpack_stardist
                from tempo_preprocessing import preprocess
                configure_tensorflow()
                with tempfile.TemporaryDirectory() as folder:
                    model = unpack_stardist(model_path, folder)
                    with zipfile.ZipFile(model_path) as archive:
                        metadata = json.loads(archive.read('model/tempo_nuclei.json'))
                    masks, details = model.predict_instances(preprocess(image, metadata['preprocessing']['profile']))
            else:
                from tempo_cellpose import predict_batch
                import torch
                device = torch.device('cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu')
                instances = predict_batch(model_path, [image], device)[0]
                boxes = []
                for instance in instances:
                    polygon = mask_polygon(instance.mask)
                    if polygon:
                        row, column = instance.bounds
                        box = dict(annotation_class(instance.class_id) if instance.class_id is not None else {'class_id': 0},
                                   polygon=[[x + column.start, y + row.start] for x, y in polygon])
                        if instance.class_id is None:
                            box['requires_viability_review'] = True
                        boxes.append(box)
                response = {'success': True, 'result_kind':'cells', 'boxes': boxes, 'model_used': os.path.basename(model_path)}
                print(json.dumps(response))
                return response
            boxes = []
            for instance_id in np.unique(masks):
                if instance_id == 0:
                    continue
                polygon = mask_polygon(masks == instance_id)
                if polygon:
                    boxes.append({'class_id':2, 'object_type':'nucleus', 'polygon':polygon, 'proposal':True})
            response = {'success':True, 'result_kind':'nuclei', 'boxes':boxes, 'model_used':os.path.basename(model_path)}
            print(json.dumps(response))
            return response
        from ultralytics import YOLO
        model = YOLO(model_path)
        
        metadata = getattr(getattr(model,'model',None), 'tempo_metadata', {})
        if metadata.get('preprocessing'):
            from tempo_preprocessing import preprocess
            from tempo_training import read_image_plane
            raw = read_image_plane(target_path) if isinstance(target_path,str) else target_path
            image = np.rint(preprocess(raw, metadata['preprocessing']['profile'])*255).astype(np.uint8)
            target_path = cv2.cvtColor(image,cv2.COLOR_GRAY2BGR)
        results = model(target_path, conf=conf, iou=iou, retina_masks=True, verbose=False)
        
        output_boxes = []
        for result in results:
            if result.masks is not None:
                for i, mask in enumerate(result.masks.data.byte().cpu().numpy()):
                    polygon = mask_polygon(mask, result.orig_shape)
                    if polygon is None:
                        continue
                    cls_id = int(result.boxes.cls[i].item())
                    score = float(result.boxes.conf[i].item()) if result.boxes.conf is not None else 0.5
                    poly_arr = [[float(pt[0]), float(pt[1])] for pt in polygon]
                    xs = [pt[0] for pt in poly_arr]
                    ys = [pt[1] for pt in poly_arr]
                    x1, x2 = min(xs), max(xs) if xs else 0.0
                    y1, y2 = min(ys), max(ys) if ys else 0.0
                    
                    output_boxes.append({
                        'class_id': cls_id,
                        'conf': score,
                        'polygon': poly_arr,
                        'box': [float(x1), float(y1), float(x2 - x1), float(y2 - y1)]
                    })
            elif result.boxes is not None:
                for box in result.boxes:
                    cls_id = int(box.cls.item())
                    score = float(box.conf[0].item()) if box.conf is not None else 0.5
                    xyxy = box.xyxy[0].tolist()
                    x1, y1, x2, y2 = xyxy
                    w = x2 - x1
                    h = y2 - y1
                    poly = [[float(x1), float(y1)], [float(x2), float(y1)], [float(x2), float(y2)], [float(x1), float(y2)]]
                    output_boxes.append({
                        'class_id': cls_id,
                        'conf': score,
                        'box': [float(x1), float(y1), float(w), float(h)],
                        'polygon': poly
                    })
                
        for box in output_boxes:
            box.update(annotation_class(box['class_id']))
        for box in output_boxes:
            if box.get('object_type','cell') == 'cell':
                x,y,w,h = box['box']
                box['center'] = [x+w/2,y+h/2]
        counts = region_counts(output_boxes)
        resp = {"success": True, "result_kind":"scout", "counts":counts,
                "exact_total_available": not bool(counts['healthy_groups'] or counts['dead_groups']), "boxes": output_boxes, "model_used": os.path.basename(model_path)}
        print(json.dumps(resp))
        return resp
        
    except Exception as e:
        err_dict = {"success": False, "error": str(e)}
        print(json.dumps(err_dict))
        return err_dict
    finally:
        if temp_img and os.path.exists(temp_img):
            try: os.remove(temp_img)
            except OSError: pass

def detect_cells_via_bridge(image_or_video_path, model_path_or_type=None, conf=0.15, iou=0.40):
    """Bridge function that can be called from Rosetta (x86_64) or native ARM64 to safely run YOLO."""
    model_path = resolve_model_path(model_path_or_type)
    target_path = os.path.abspath(image_or_video_path) if isinstance(image_or_video_path, str) else image_or_video_path

    try:
        venv_native = os.path.join(BASE_DIR, ".venv", "bin", "python")
        
        # If already running natively on ARM64 or Windows, invoke directly without subprocess
        if ".venv_x86" not in sys.executable:
            return run_inference(target_path, model_path, conf=conf, iou=iou)

        cmd_prefix = []
        native_python = sys.executable
        if os.path.exists(venv_native):
            native_python = venv_native
            if platform.system() == "Darwin":
                cmd_prefix = ["arch", "-arm64"]
            
        helper_script = os.path.join(BASE_DIR, "yolo_inference_helper.py")
        cmd = cmd_prefix + [native_python, helper_script, target_path, model_path, str(conf), str(iou)]
        
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        output_lines = result.stdout.strip().split('\n')
        json_response = None
        for line in reversed(output_lines):
            if line.startswith('{"success":'):
                json_response = json.loads(line)
                break
                
        if not json_response:
            return {"success": False, "error": f"Could not parse inference JSON:\n{result.stdout}\n{result.stderr}"}
            
        return json_response
        
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="TEMPO YOLO & Edge Model Inference Bridge")
    parser.add_argument("input_path", type=str, help="Path to input image or video")
    parser.add_argument("model_or_conf", nargs="?", default=None, help="Model path, alias, or positional conf")
    parser.add_argument("pos_conf", nargs="?", default=None, help="Positional conf if model provided")
    parser.add_argument("pos_iou", nargs="?", default=None, help="Positional iou")
    parser.add_argument("--model_type", type=str, default=None, help="Explicit model type alias (scout, stardist, cellpose)")
    parser.add_argument("--conf", type=float, default=None, help="Confidence threshold")
    parser.add_argument("--iou", type=float, default=None, help="IoU threshold")

    args = parser.parse_args()

    # Determine model target
    target_model = args.model_type or args.model_or_conf or "cellpose"
    conf_val = None
    try:
        if args.model_or_conf is not None and not args.model_type and not args.pos_conf:
            conf_val = float(args.model_or_conf)
            target_model = "cellpose"
    except ValueError:
        pass

    # Determine conf and iou
    if args.conf is not None:
        final_conf = args.conf
    elif args.pos_conf is not None:
        try: final_conf = float(args.pos_conf)
        except ValueError: final_conf = 0.15
    elif conf_val is not None:
        final_conf = conf_val
    else:
        final_conf = 0.15

    if args.iou is not None:
        final_iou = args.iou
    elif args.pos_iou is not None:
        try: final_iou = float(args.pos_iou)
        except ValueError: final_iou = 0.40
    else:
        final_iou = 0.40

    resolved_path = resolve_model_path(target_model)
    run_inference(args.input_path, resolved_path, conf=final_conf, iou=final_iou)
