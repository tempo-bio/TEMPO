"""Class-specific Cellpose checkpoints and conservative prediction merging.

The published .pt artifact is a versioned TEMPO bundle containing native
Cellpose checkpoints for healthy/dead cells and, when trained, groups. Legacy single checkpoints remain outline-only.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import gc
import json
import logging
from pathlib import Path
import re
import tempfile
from typing import Optional, Tuple
import zipfile

import cv2
import numpy as np
from scipy.ndimage import find_objects
from tempo_regions import MODEL_CLASSES

FORMAT = 'tempo-cellpose-specialists-v1'
GROUP_FORMAT = 'tempo-cellpose-specialists-v2'
ASSISTED_FORMAT = 'tempo-cellpose-specialists-v3'
MANIFEST = 'tempo_cellpose.json'
CLASSES = {'healthy': 0, 'dead': 1}
CONFLICT_OVERLAP = .5


def valid_specialist_classes(names):
    names = set(names)
    return (names <= set(MODEL_CLASSES) and bool(names & {'healthy', 'healthy_group'})
            and bool(names & {'dead', 'dead_group'}))


@dataclass
class CellInstance:
    class_id: Optional[int]  # None means the specialists disagree, or legacy weights.
    mask: np.ndarray
    bounds: Tuple[slice, slice]


def specialist_labels(masks, label_files):
    """Rasterize each class independently from approved segmentation polygons.

    Instance numbers alone do not encode healthy/dead. Filtering the combined
    instance image would also discard target pixels overwritten by another
    class. Keep each class's original geometry and zero-target negative images.
    """
    if len(masks) != len(label_files):
        raise ValueError('Cellpose masks and class-label files do not match.')
    result = {name: [] for name in MODEL_CLASSES}
    for mask, path in zip(masks, label_files):
        if mask is None or mask.ndim != 2 or not np.issubdtype(mask.dtype, np.integer) or np.any(mask < 0):
            raise ValueError('Cellpose requires a two-dimensional instance-label image.')
        classes, polygons = [], []
        for line in Path(path).read_text().splitlines():
            if line.strip():
                token = line.split()[0]
                if token not in ('0', '1', '2', '3'):
                    raise ValueError('Unknown Cellpose class label; rebuild the dataset.')
                classes.append(int(token))
                try:
                    points = np.asarray([float(value) for value in line.split()[1:]], dtype=np.float64).reshape(-1, 2)
                except ValueError as exc:
                    raise ValueError('Invalid Cellpose segmentation polygon; rebuild the dataset.') from exc
                if len(points) < 3 or not np.isfinite(points).all() or np.any(points < 0) or np.any(points > 1):
                    raise ValueError('Invalid Cellpose segmentation polygon; rebuild the dataset.')
                polygons.append(points)
        if int(mask.max()) > len(classes):
            raise ValueError('Rebuild the dataset: Cellpose instance IDs have no matching class labels.')
        for name, class_id in MODEL_CLASSES.items():
            labels = np.zeros(mask.shape, dtype=np.uint16)
            height, width = mask.shape
            instance_id = 0
            for cid, points in zip(classes, polygons):
                if cid == class_id:
                    instance_id += 1
                    pixels = np.rint(points * [width - 1, height - 1]).astype(np.int32)
                    cv2.fillPoly(labels, [pixels], instance_id)
            result[name].append(labels)
    return result


def save_specialists(target, checkpoints, *, profile=None, helper=None):
    """Install the specialists atomically; require both healthy and dead targets."""
    target = Path(target)
    if not valid_specialist_classes(checkpoints):
        raise ValueError('Healthy and dead Cellpose checkpoints are required, for cells or groups.')
    for path in checkpoints.values():
        if not Path(path).is_file() or Path(path).stat().st_size == 0:
            raise ValueError('Cellpose training did not produce every required checkpoint.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            classes = {name: MODEL_CLASSES[name] for name in checkpoints}
            manifest = {'format': ASSISTED_FORMAT if profile is not None else GROUP_FORMAT if set(classes)-set(CLASSES) else FORMAT,
                        'classes': classes, 'conflict_overlap': CONFLICT_OVERLAP}
            if profile is not None:
                from tempo_preprocessing import metadata
                manifest['preprocessing'] = metadata(profile)
                manifest['channels'] = {'healthy': ['image','nuclei','zero'] if helper else ['image']}
                if helper:
                    import hashlib
                    path = Path(helper['path'])
                    if hashlib.sha256(path.read_bytes()).hexdigest() != helper['sha256']:
                        raise ValueError('Nuclear helper changed before Cellpose export.')
                    manifest['helper_sha256'] = helper['sha256']
                    archive.write(path, 'nuclei.zip')
            archive.writestr(MANIFEST, json.dumps(manifest))
            for name, path in checkpoints.items():
                archive.write(path, name + '.pt')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def checkpoint_paths(path):
    """Distinguish our bundle from PyTorch's own zip serialization."""
    path = Path(path)
    if not zipfile.is_zipfile(path):
        yield {'unclassified': path}
        return
    with zipfile.ZipFile(path) as archive:
        if MANIFEST not in archive.namelist():
            yield {'unclassified': path}
            return
        manifest = _validated_manifest(archive)
        classes = manifest["classes"]
        with tempfile.TemporaryDirectory(prefix='tempo-cellpose-') as folder:
            paths = {}
            for name in classes:
                member = name + '.pt'
                if archive.getinfo(member).file_size == 0:
                    raise ValueError('Empty Cellpose specialist checkpoint.')
                archive.extract(member, folder)  # Exact, fixed names validated above.
                paths[name] = Path(folder) / member
            yield paths


def _validate_mask(mask):
    mask = np.asarray(mask)
    if mask.ndim != 2 or not np.issubdtype(mask.dtype, np.integer) or np.any(mask < 0):
        raise ValueError('Cellpose returned invalid instance labels.')
    # Bound label IDs and make sparse labels contiguous before find_objects.
    _, indices = np.unique(mask, return_inverse=True)
    if not np.any(mask == 0):
        indices += 1
    return indices.reshape(mask.shape).astype(np.int32)


def merge_specialists(healthy, dead):
    return merge_predictions({0: healthy, 1: dead})


def merge_predictions(predictions):
    """Conflicting class or cell/group claims become one unclassified region."""
    sources = {cid: _validate_mask(mask) for cid, mask in predictions.items()}
    if len({mask.shape for mask in sources.values()}) != 1:
        raise ValueError('Cellpose specialist masks have different dimensions.')
    boxes = {cid: find_objects(mask) for cid, mask in sources.items()}
    nodes = [(cid, index + 1) for cid in sources for index, bounds in enumerate(boxes[cid]) if bounds is not None]
    parents = {node: node for node in nodes}
    def root(node):
        while parents[node] != node:
            parents[node] = parents[parents[node]]
            node = parents[node]
        return node
    areas = {cid: np.bincount(mask.ravel()) for cid, mask in sources.items()}
    for left in sources:
        for right in sources:
            if left >= right:
                continue
            a, b = sources[left], sources[right]
            overlap = (a > 0) & (b > 0)
            if overlap.any():
                pairs, counts = np.unique(np.column_stack((a[overlap], b[overlap])), axis=0, return_counts=True)
                for (aid, bid), count in zip(pairs, counts):
                    if count / min(areas[left][aid], areas[right][bid]) >= CONFLICT_OVERLAP:
                        parents[root((right, int(bid)))] = root((left, int(aid)))
    groups = {}
    for node in nodes:
        groups.setdefault(root(node), []).append(node)
    instances = []
    for group in groups.values():
        spans = [boxes[cid][index - 1] for cid, index in group]
        bounds = tuple(slice(min(span[axis].start for span in spans), max(span[axis].stop for span in spans))
                       for axis in (0, 1))
        mask = np.zeros(tuple(span.stop - span.start for span in bounds), dtype=np.uint8)
        for cid in sources:
            ids = [index for source, index in group if source == cid]
            if ids:
                mask |= np.isin(sources[cid][bounds], ids)
        instances.append(CellInstance(group[0][0] if len(group) == 1 else None, mask, bounds))
    return instances


def unclassified_instances(labels):
    labels = _validate_mask(labels)
    return [CellInstance(None, (labels[bounds] == index + 1).astype(np.uint8), bounds)
            for index, bounds in enumerate(find_objects(labels)) if bounds is not None]


def release_model_memory(device):
    import torch
    gc.collect()
    if str(device).startswith('cuda'):
        torch.cuda.empty_cache()
    elif str(device).startswith('mps'):
        torch.mps.empty_cache()


@contextmanager
def negative_image_diameters(training_labels):
    """Give empty images finite size metadata without inventing foreground.

    Cellpose's median diameter is NaN for an empty label image. Use the mean
    diameter of positive training images for those bookkeeping entries, leaving
    the original masks, empty mask count, and segmentation loss unchanged.
    The override is scoped to one isolated training worker and always restored.
    """
    from cellpose import utils
    original = utils.diameters
    values = [float(original(mask)[0]) for mask in training_labels if np.any(mask > 0)]
    finite = [value for value in values if np.isfinite(value) and value > 0]
    diameter = max(5., float(np.mean(finite))) if finite else 5.
    def diameters(mask):
        if not np.any(np.asarray(mask) > 0):
            return diameter, np.empty(0, dtype=np.float32)
        return original(mask)
    utils.diameters = diameters
    try:
        yield
    finally:
        utils.diameters = original


def train_specialists(train_images, train_labels, val_images, val_labels, output, epochs, batch, device, *, helper=None, profile=None, strict=False, seed=17):
    """Start each specialist independently from the same pretrained family."""
    from tempo_progress import report as progress
    from tempo_model_catalog import MODEL_FILES
    if (not train_images or not val_images or not set(CLASSES) <= set(train_labels) <= set(MODEL_CLASSES)
            or not set(CLASSES) <= set(val_labels) <= set(MODEL_CLASSES)
            or any(len(masks) != len(train_images) for masks in train_labels.values())
            or any(len(masks) != len(val_images) for masks in val_labels.values())):
        raise ValueError('Cellpose training and validation images must have matching specialist labels.')
    counts = {name: sum(len(np.unique(mask[mask > 0])) for mask in train_labels.get(name, [])) for name in MODEL_CLASSES}
    missing = [name for name in CLASSES if counts[name] + counts[name + '_group'] == 0]
    if missing:
        missing = ', '.join(missing)
        raise ValueError(f'Cellpose specialists need approved training examples of both classes; missing {missing}. '
                         'Add reviewed examples to the training partition and rebuild the dataset.')
    active = [name for name in MODEL_CLASSES if counts[name] > 0]
    if helper and 'healthy' not in active:
        raise ValueError('Nuclear-assisted Cellpose requires approved healthy individual-cell outlines.')
    for name in active:
        counts[name] = sum(len(np.unique(mask[mask > 0])) for mask in train_labels[name])
        if name not in val_labels:
            val_labels[name] = [np.zeros_like(mask) for mask in val_labels['healthy']]
    from cellpose import models, train
    import torch
    output = Path(output)
    checkpoints, stages = {}, {}
    for stage_index, name in enumerate(active):
        torch.manual_seed(seed + MODEL_CLASSES[name])
        np.random.seed(seed + MODEL_CLASSES[name])
        progress('cellpose', 'training_' + name, stage_index * epochs, len(active) * epochs)
        selected_model, fallback = 'cpdino', None
        try:
            model = models.CellposeModel(gpu=str(device).startswith('cuda'), device=torch.device(device), pretrained_model='cpdino')
        except Exception as exc:
            if strict:
                raise RuntimeError('CPDINO is required for controlled training. Use Python 3.11+ with the pinned DINOv3 dependency or the maintained Colab environment.') from exc
            selected_model = 'cpsam'
            fallback = dict(from_model='cpdino', to_model='cpsam', error_type=type(exc).__name__)
            print(f'CPDINO initialization failed ({type(exc).__name__}); loading Cellpose-SAM for {name}.', flush=True)
            model = models.CellposeModel(gpu=str(device).startswith('cuda'), device=torch.device(device), pretrained_model='cpsam')
        stages[name] = dict(pretrained_model=selected_model,
                           backbone=str(getattr(model.net, 'backbone', None) or selected_model),
                           fallback=fallback, training_cells=counts[name], epochs=epochs)
        print(f'Cellpose {name}: {counts[name]} target outlines in {len(train_images)} images; '
              f'backbone {stages[name]["backbone"]}; retaining zero-target negative images.', flush=True)
        class CellposeProgress(logging.Handler):
            def emit(self, record):
                match = re.match(r'^(\d+), train_loss=', record.getMessage())
                if match:
                    progress('cellpose', 'training_' + name, stage_index * epochs + int(match.group(1)) + 1, len(active) * epochs)
        logger = logging.getLogger('cellpose.train')
        handler, previous_level = CellposeProgress(), logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        folder = output / name
        folder.mkdir(parents=True, exist_ok=True)
        try:
            # Opposite-class cells remain in the source image, with target label
            # zero. Do not drop images containing only the opposite class.
            # No override is needed when both partitions contain target cells
            # in every image. Keep Cellpose's normal bookkeeping in that case.
            has_negatives = any(not np.any(mask > 0) for mask in train_labels[name] + val_labels[name])
            with negative_image_diameters(train_labels[name]) if has_negatives else nullcontext():
                from tempo_nuclei import helper_channels
                tx = [image.astype(np.float32)/255 for image in train_images] if profile else train_images
                vx = [image.astype(np.float32)/255 for image in val_images] if profile else val_images
                guided = bool(helper and name == 'healthy')
                if guided:
                    tx = [helper_channels(image,cue) for image,cue in zip(tx,helper['train'])]
                    vx = [helper_channels(image,cue) for image,cue in zip(vx,helper['val'])]
                with helper_dropout(model.net) if guided else nullcontext():
                    result = train.train_seg(model.net, train_data=tx, train_labels=train_labels[name],
                                         test_data=vx, test_labels=val_labels[name], channel_axis=-1 if guided else None,
                                         **({'normalize': False, 'save_every':25, 'save_each':True} if profile else {}),
                                         n_epochs=epochs, batch_size=batch, min_train_masks=0,
                                         save_path=str(folder), model_name='cellpose_' + name)
            checkpoints[name] = Path(result[0] if isinstance(result, (tuple, list)) else result)
            if profile:
                from tempo_training import instance_counts
                candidates = sorted(checkpoints[name].parent.glob(checkpoints[name].name+'_epoch_*')) + [checkpoints[name]]
                best_score = -1
                for candidate in candidates:
                    model.net.load_model(str(candidate), device=torch.device(device))
                    matched = np.zeros(3,dtype=int)
                    for image, truth in zip(vx,val_labels[name]):
                        prediction = model.eval(image,normalize=False,channel_axis=-1 if guided else None)[0]
                        matched += instance_counts(truth,prediction)
                    score = 2*matched[0]/max(1,2*matched[0]+matched[1]+matched[2])
                    if score > best_score:
                        best_score=score;checkpoints[name]=candidate
                stages[name]['validation_f1']=float(best_score)
                stages[name]['checkpoint']=checkpoints[name].name
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
            del model
            release_model_memory(device)
    target = output / MODEL_FILES['cellpose']
    save_specialists(target, checkpoints, profile=profile, helper=helper)
    details = dict(strategy=GROUP_FORMAT if set(active) - set(CLASSES) else FORMAT, stages=stages, epochs_per_specialist=epochs,
                   conflict_overlap=CONFLICT_OVERLAP)
    (output / 'training_details.json').write_text(json.dumps(details, indent=2))
    return str(target)


def predict_batch(path, images, device, *, processed=False, helper_mode="normal", cue_output=None,
                  progress=None):
    """Run one specialist at a time, sharing the inference path with evaluation."""
    from cellpose import models
    import torch
    outputs = {}
    manifest = bundle_metadata(path)
    profile = manifest.get('preprocessing', {}).get('profile')
    if profile:
        from tempo_preprocessing import preprocess
        images = [image.astype(np.float32)/255 if processed else preprocess(image,profile) for image in images]
    cues = None
    if manifest.get('helper_sha256'):
        if progress is not None:
            progress('nuclear')
        from tempo_nuclear_training import predict_maps_isolated
        import hashlib
        with tempfile.TemporaryDirectory() as folder, zipfile.ZipFile(path) as archive:
            content = archive.read('nuclei.zip')
            if hashlib.sha256(content).hexdigest() != manifest['helper_sha256']:
                raise ValueError('Cellpose nuclear helper checksum mismatch.')
            nuclear = Path(folder)/'nuclei.zip'; nuclear.write_bytes(content)
            cues = predict_maps_isolated(nuclear,images)
        if helper_mode == 'zero': cues = [np.zeros_like(cue) for cue in cues]
        elif helper_mode == 'displaced': cues = [np.roll(cue,20,axis=1) for cue in cues]
        elif helper_mode != 'normal': raise ValueError('Unknown helper diagnostic mode.')
        if cue_output is not None:
            Path(cue_output).mkdir(parents=True,exist_ok=True)
            for index,cue in enumerate(cues):
                if not cv2.imwrite(str(Path(cue_output)/(str(index)+'-nuclei.png')),np.rint(cue*255).astype(np.uint8)):
                    raise OSError('Could not save the nuclear helper preview.')
    with checkpoint_paths(path) as paths:
        for name, checkpoint in paths.items():
            model = models.CellposeModel(pretrained_model=str(checkpoint), device=torch.device(device),
                                         gpu=str(device).startswith('cuda'),
                                         # BF16 arithmetic is emulated on many CPUs, including
                                         # Ryzen Zen 3. Keep GPU precision at Cellpose's default.
                                         use_bfloat16=torch.device(device).type != 'cpu')
            try:
                predictions = []
                for index, image in enumerate(images):
                    guided = cues is not None and name == 'healthy'
                    if guided:
                        from tempo_nuclei import helper_channels
                        image = helper_channels(image,cues[index])
                    if progress is not None:
                        progress('inference')
                    labels = _validate_mask(model.eval(image, channel_axis=-1 if guided else None,
                                                      **({'normalize':False} if profile else {}))[0])
                    if labels.shape != image.shape[:2]:
                        raise ValueError('Cellpose prediction and image dimensions differ.')
                    predictions.append(labels)
                outputs[name] = predictions
            finally:
                del model
                release_model_memory(device)
    if 'unclassified' in outputs:
        return [unclassified_instances(mask) for mask in outputs['unclassified']]
    return [merge_predictions({MODEL_CLASSES[name]: masks[index] for name, masks in outputs.items()})
            for index in range(len(images))]


def instance_image(instances, shape, class_id=None):
    """Rasterize one class for matching; None includes all outlines."""
    labels = np.zeros(shape, dtype=np.int32)
    for index, instance in enumerate(instances, 1):
        if class_id is None or instance.class_id == class_id:
            labels[instance.bounds][instance.mask > 0] = index
    return labels


def _validated_manifest(archive):
    names=archive.namelist()
    if len(names)!=len(set(names)) or archive.getinfo(MANIFEST).file_size>4096:
        raise ValueError('Invalid Cellpose specialist bundle contents.')
    manifest=json.loads(archive.read(MANIFEST));classes=manifest.get('classes',{})
    if (not isinstance(classes,dict) or not valid_specialist_classes(classes)
            or classes!={name:MODEL_CLASSES[name] for name in classes}
            or manifest.get('format') not in (ASSISTED_FORMAT,GROUP_FORMAT if set(classes)-set(CLASSES) else FORMAT)
            or manifest.get('conflict_overlap')!=CONFLICT_OVERLAP
            or set(names)!=({MANIFEST,*(name+'.pt' for name in classes)}|({'nuclei.zip'} if manifest.get('helper_sha256') else set()))):
        raise ValueError('Unsupported Cellpose specialist bundle.')
    if manifest['format']==ASSISTED_FORMAT:
        from tempo_preprocessing import metadata
        preprocessing=manifest.get('preprocessing',{})
        if preprocessing!=metadata(preprocessing.get('profile')):
            raise ValueError('Unsupported Cellpose preprocessing.')
        helper=manifest.get('helper_sha256')
        if helper and (not isinstance(helper,str) or not re.fullmatch('[0-9a-f]{64}',helper)):
            raise ValueError('Invalid nuclear helper identity.')
        expected=['image','nuclei','zero'] if helper else ['image']
        if manifest.get('channels')!={'healthy':expected}:
            raise ValueError('Unsupported Cellpose input channels.')
    return manifest


def bundle_metadata(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if MANIFEST in archive.namelist():
                return _validated_manifest(archive)
    return {}


@contextmanager
def helper_dropout(net):
    """Perturb only the nuclear input in training; leave validation untouched."""
    import torch
    def modify(module, args):
        if not module.training:
            return args
        values = args[0].clone()
        gain = .5 + .5 * torch.rand((len(values),1,1), device=values.device)
        gain *= (torch.rand((len(values),1,1), device=values.device) >= .3)
        values[:,1] *= gain
        return (values, *args[1:])
    handle = net.register_forward_pre_hook(modify)
    try:
        yield
    finally:
        handle.remove()
