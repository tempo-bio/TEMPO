"""Approved training datasets, model adapters, and held-out evaluation."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import tempfile
import zipfile
import cv2
import numpy as np
from tempo_annotations import normalize_record, validate_annotations
from tempo_model_catalog import MODEL_FILES
from tempo_preprocessing import preprocess, metadata as preprocessing_metadata
from tempo_nuclei import is_nucleus, nuclear_labels, attach_supplement
from tempo_regions import MODEL_CLASSES, model_class
from tempo_training_groups import SPLIT_POLICY, plan_splits, validate_assignments, partition_settings

APPROVAL_POLICY = 'explicit-approved-positive-v1'
DATASET_FORMAT = 'tempo-training-v3'


def read_image_plane(path, flags=cv2.IMREAD_GRAYSCALE):
    """Decode one image plane without Ultralytics' Windows imread replacement.

    That replacement adds a channel axis even to grayscale images and TIFF
    masks. Keep OpenCV's requested shape and mask dtype, including Unicode paths.
    """
    try:
        image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), flags)
    except (OSError, cv2.error):
        return None
    return image


def select_approved_annotations(records):
    """Saved drafts and legacy records are never evidence of completed review."""
    if not isinstance(records, dict):
        raise ValueError('Annotations must be a mapping of image names to records.')
    approved = {}
    identities = set()
    excluded = 0
    for name, record in records.items():
        if not isinstance(record, dict):
            raise ValueError(f'{name}: invalid annotation record.')
        if record.get('status') != 'approved':
            excluded += 1
            continue
        identity = record.get('image_id')
        if identity:
            if identity in identities:
                raise ValueError(f'{name}: duplicate approved image identity; reconcile the dataset before training.')
            identities.add(identity)
        if not any(box.get('class_id') in (0, 1) or box.get('class_id') == -1 or box.get('dead')
                   for box in record.get('boxes', [])):
            raise ValueError(f'{name}: marked approved but has no cell annotations. '
                             'All TEMPO training images must have cells; review this image again.')
        approved[name] = record
    print(f'Approved images: {len(approved)}. Excluded draft or unreviewed images: {excluded}.', flush=True)
    if not approved:
        raise ValueError('No explicitly approved images are available. Saving annotations or adding some '
                         'polygons does not mark an image complete. Finish and approve whole images before training.')
    return approved


def validate_training_dataset(dataset, *, allow_training_check=False):
    info = json.loads((Path(dataset)/'dataset.json').read_text())
    if info.get('approval_policy') != APPROVAL_POLICY:
        raise ValueError('This compiled dataset predates explicit approval checks. '
                         'Rebuild it from approved annotations before training.')
    if info.get('format') != DATASET_FORMAT:
        raise ValueError('Rebuild the dataset for centroid and partial nuclear training.')
    if info.get('purpose') == 'training-check' and not allow_training_check:
        raise ValueError('Training-check data cannot be used for a publishable run. Prepare the full training dataset.')
    if info.get('purpose') != 'training-check' and info.get('split_policy') != SPLIT_POLICY:
        raise ValueError('Rebuild the dataset with current image assignments before training.')
    return info


def _install_dataset(staging, output, assignments, split_file, policy_file, groups):
    """Keep the prior compilation and holdouts recoverable until all writes settle."""
    folder = Path(tempfile.mkdtemp(prefix='.dataset-install-', dir=output.parent))
    retain_backups = False
    backups, attempted = {}, []
    try:
        updates = [(output, staging)]
        for target, value in ((split_file, assignments), (policy_file, {'approval_policy': APPROVAL_POLICY,
                              'split_policy': SPLIT_POLICY, 'source_groups': groups})):
            candidate = folder/target.name
            with candidate.open('w') as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.flush(); os.fsync(stream.fileno())
            updates.append((target, candidate))
            if target.exists():
                backup = folder/(target.name+'.previous')
                shutil.copy2(target, backup)
                backups[target] = backup
        try:
            if output.exists():
                backup = folder/'compiled.previous'
                backups[output] = backup
                os.replace(output, backup)
            for target, candidate in updates:
                attempted.append(target)
                os.replace(candidate, target)
        except BaseException as installation_error:
            retain_backups = True
            rollback_errors = []
            # Output may have moved to backup before its replacement failed.
            targets = list(reversed(attempted))
            if output in backups and output not in targets:
                targets.append(output)
            for target in targets:
                try:
                    if target == output and target in backups and not backups[target].exists():
                        continue  # Moving the old output failed before changing it.
                    if (target != output and target in backups and target.is_file()
                            and target.read_bytes() == backups[target].read_bytes()):
                        continue  # No metadata change occurred before the failure.
                    if target == output and target.exists():
                        shutil.rmtree(target)
                    if target in backups:
                        os.replace(backups[target], target)
                    elif target.exists():
                        target.unlink()
                except BaseException as exc:
                    rollback_errors.append(f'{target.name}: {type(exc).__name__}: {exc}')
            if rollback_errors:
                raise RuntimeError(f'Dataset installation failed; rollback could not finish. Dataset backups are retained in {folder}. '
                                   + ' '.join(rollback_errors)) from installation_error
            retain_backups = False
            raise
    finally:
        if not retain_backups:
            shutil.rmtree(folder)


def build_dataset(base_dir, origin="approved-master", *, source_groups=None, split_assignments=None, training_check=False, profile=None, box_width=None, assisted=None):
    root = Path(base_dir)
    candidates = [root/'datasets/universal_annotations.json', root/'datasets/universal_images/universal_annotations.json', root/'universal_annotations.json']
    source = next((p for p in candidates if p.exists()), None)
    if source is None:
        raise ValueError('Approved universal_annotations.json is required.')
    raw = json.loads(source.read_text())
    supplements_file = source.parent / "nuclear_annotations.json"
    supplements = json.loads(supplements_file.read_text()) if supplements_file.exists() else {}
    raw = {name: attach_supplement(record, supplements.get(record.get("image_id"))) for name, record in raw.items()}
    approved = select_approved_annotations(raw)
    groups = source_groups or {}
    validation_file = source.parent/'architecture_validation.json'
    architecture_validation = json.loads(validation_file.read_text()) if validation_file.is_file() else {}
    if assisted is None:
        assisted=architecture_validation.get('nuclear_assistance_passed',True)
    scout_profile = profile or architecture_validation.get('scout_profile',architecture_validation.get('profile','P0'))
    profile = profile or architecture_validation.get('profile','P0')
    box_width = box_width or architecture_validation.get('box_width',16)
    preprocessing_metadata(profile)
    preprocessing_metadata(scout_profile)
    if box_width not in (8,16,24):
        raise ValueError('Scout box width must be 8, 16 or 24 reference pixels.')
    output = root/'datasets'/('compiled-check' if training_check else 'compiled')
    split_file = root/'datasets'/('check_splits.json' if training_check else 'training_splits.json')
    assignments = json.loads(split_file.read_text()) if split_file.exists() else {}
    previous_policy = None
    split_policy_file = root/'datasets'/('check_split_policy.json' if training_check else 'training_split_policy.json')
    previous_groups = {}
    if split_policy_file.exists():
        saved_policy = json.loads(split_policy_file.read_text())
        previous_policy = saved_policy.get('approval_policy')
        previous_groups = saved_policy.get('source_groups', {})
    if (output/'dataset.json').exists():
        previous_policy = json.loads((output/'dataset.json').read_text()).get('approval_policy')
    if assignments and previous_policy != APPROVAL_POLICY:
        # Old splits included drafts and cannot define this approved-only experiment.
        backup = split_file.with_name('training_splits.before-approval-filter.json')
        if not backup.exists():
            shutil.copy2(split_file, backup)
        print('Starting approved-only splits; previous assignments backed up.', flush=True)
        assignments = {}
    ordered = sorted(approved, key=lambda n:hashlib.sha256(n.encode()).hexdigest())
    assignments = plan_splits(approved, groups, split_assignments if split_assignments is not None else assignments, previous_groups)
    if training_check:
        ordered = [name for name in ordered if assignments[name] == 'train']
        if not ordered:
            raise ValueError('No training images are available for Check Training. Approve images from a training well.')
    # Rebuild every run so new approvals and corrected annotations cannot leave stale labels.
    staging = Path(tempfile.mkdtemp(prefix='compiled-', dir=root/'datasets'))
    records = []
    try:
        for name in ordered:
            record = approved[name]
            clean_name = re.sub(r'^\d{8}_\d{6}_', '', name)
            image = next((p for d in (root/'datasets/images', root/'datasets/universal_images', root/'datasets', root/'images')
                          for p in (d/name, d/clean_name) if p.is_file()), None)
            if image is None:
                raise ValueError(f'Missing approved image: {name}')
            gray = read_image_plane(image)
            if gray is None:
                raise ValueError(f'Unreadable image: {name}')
            h,w = gray.shape
            model_image = np.rint(preprocess(gray, profile) * 255).astype(np.uint8)
            scout_image = model_image if scout_profile==profile else np.rint(preprocess(gray,scout_profile)*255).astype(np.uint8)
            normalized = normalize_record(record, w, h)
            validate_annotations({name:normalized})
            split = assignments[name]
            if split not in ('train','val','test'):
                raise ValueError('Invalid persisted dataset split.')
            stem = hashlib.sha256(name.encode()).hexdigest()[:20]
            labels = np.zeros((h,w), np.uint16)
            seg, det, centers, group_polygons = [], [], [], []
            index = 0
            for box in normalized['boxes']:
                if is_nucleus(box):
                    continue
                index += 1
                points = np.asarray(box['polygon'], dtype=np.float64)
                pixels = np.rint(points * [w-1,h-1]).astype(np.int32)
                cv2.fillPoly(labels, [pixels], index)
                cid = model_class(box)
                seg.append(str(cid) + ' ' + ' '.join(f'{v:.7f}' for v in points.ravel()))
                x0,y0 = points.min(axis=0); x1,y1 = points.max(axis=0)
                if box.get('object_type') == 'group':
                    group_polygons.append(points.tolist())
                    cx, cy, bw, bh = (x0+x1)/2, (y0+y1)/2, x1-x0, y1-y0
                else:
                    from tempo_nuclei import raster_polygon
                    moments = cv2.moments(raster_polygon(box, (h, w)))
                    if not moments['m00']:
                        raise ValueError('A cell mask is empty at source resolution.')
                    px, py = moments['m10']/moments['m00'], moments['m01']/moments['m00']
                    half = box_width * max(w, h) / 640 / 2
                    left, right = max(0, px-half), min(w, px+half)
                    top, bottom = max(0, py-half), min(h, py+half)
                    cx, cy, bw, bh = (left+right)/2/w, (top+bottom)/2/h, (right-left)/w, (bottom-top)/h
                    centers.append(dict(class_id=cid, center=[px,py], clipped=left == 0 or top == 0 or right == w or bottom == h))
                det.append(f"{cid} {cx:.7f} {cy:.7f} {bw:.7f} {bh:.7f}")
            for task, lines in [('cells',seg),('detect',det)]:
                images = staging/task/'images'/split; annotations = staging/task/'labels'/split
                images.mkdir(parents=True, exist_ok=True); annotations.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(images/f'{stem}.png'), scout_image if task=='detect' else model_image):
                    raise OSError(f'Could not encode compiled image: {name}')
                (annotations/f'{stem}.txt').write_text('\n'.join(lines)+'\n')
            masks = staging/'masks'/split; masks.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(masks/f'{stem}.tif'), labels):
                raise OSError(f'Could not encode compiled instance labels: {name}')
            unknown = staging/'detect/unknown'/split; unknown.mkdir(parents=True, exist_ok=True)
            (unknown/f'{stem}.json').write_text(json.dumps(dict(shape=[h,w], groups=group_polygons, centers=centers)))
            originals=staging/'raw/images'/split;originals.mkdir(parents=True,exist_ok=True)
            if not cv2.imwrite(str(originals/f'{stem}.png'),gray):
                raise OSError('Could not preserve the evaluation image plane.')
            nuclear = staging/'nuclei'/split; nuclear.mkdir(parents=True, exist_ok=True)
            np.save(nuclear/f'{stem}.npy', nuclear_labels(normalized, (h,w)), allow_pickle=False)
            records.append(dict(name=name, stem=stem, split=split, image_id=normalized.get('image_id'),
                                source_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                                preparation=(source_groups or {}).get(name) or normalized.get('animal_id') or normalized.get('preparation_id'),
                                subset_shapes=[dict(shape,model_class=model_class(shape)) for shape in normalized['boxes']
                                               if not is_nucleus(shape) and shape.get('experiment_subsets')]))
        settings = partition_settings(assignments)
        if training_check:
            settings = dict(validation_partition='train', evaluation_partition='train', evaluation_basis='training-images')
        for task in ('cells','detect'):
            validation = settings['validation_partition']
            testing = settings['evaluation_partition']
            (staging/task/'data.yaml').write_text(f'path: {json.dumps(str(output/task))}\ntrain: images/train\nval: images/{validation}\ntest: images/{testing}\nnames: {json.dumps(list(MODEL_CLASSES))}\n')
        fingerprint = hashlib.sha256(json.dumps({n:approved[n] for n in ordered if assignments[n]=='test'},sort_keys=True).encode())
        for record in records:
            if record['split'] == 'test':
                fingerprint.update((staging/'cells/images/test'/f"{record['stem']}.png").read_bytes())
        (staging/'dataset.json').write_text(json.dumps({'format': DATASET_FORMAT, 'preprocessing': preprocessing_metadata(profile), 'scout_preprocessing':preprocessing_metadata(scout_profile), 'box_width': box_width, 'assisted': assisted, 'architecture_validation':architecture_validation, 'records':records,'test_fingerprint':fingerprint.hexdigest(),
                         'origin':origin,'approval_policy':APPROVAL_POLICY, 'split_policy': SPLIT_POLICY,
                         'source_groups': groups, 'crossfit_unit':'field' if architecture_validation.get('scope')=='single_mouse_fields' else 'preparation',
                         **settings, 'purpose': 'training-check' if training_check else 'training'}))
        for split in ('train', 'val', 'test'):
            label = {'train':'Training', 'val':'Validation', 'test':'Comparison'}[split]
            print(f'{label} images: {sum(r["split"] == split for r in records)}', flush=True)
        _install_dataset(staging, output, assignments, split_file, split_policy_file, groups)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output


def load_partition(dataset, split):
    paths = sorted((Path(dataset)/'cells/images'/split).glob('*.png'))
    images = [read_image_plane(p) for p in paths]
    masks = [read_image_plane(Path(dataset)/'masks'/split/(p.stem+'.tif'), cv2.IMREAD_UNCHANGED) for p in paths]
    return images, masks


def load_cellpose_partition(dataset, split):
    from tempo_cellpose import specialist_labels
    images, masks = load_partition(dataset, split)
    paths = sorted((Path(dataset) / 'cells/images' / split).glob('*.png'))
    labels = [Path(dataset) / 'cells/labels' / split / (path.stem + '.txt') for path in paths]
    return images, specialist_labels(masks, labels)


def load_nuclear_partition(dataset, split):
    images, _ = load_partition(dataset, split)
    paths = sorted((Path(dataset)/'cells/images'/split).glob('*.png'))
    labels = [np.load(Path(dataset)/'nuclei'/split/(path.stem+'.npy'), allow_pickle=False) for path in paths]
    return images, labels



def configure_tensorflow(memory_mb=None):
    global _TENSORFLOW_CONFIGURED
    if globals().get("_TENSORFLOW_CONFIGURED"):
        return
    import tensorflow as tf
    for gpu in tf.config.list_physical_devices('GPU'):
        if memory_mb:
            tf.config.set_logical_device_configuration(gpu, [tf.config.LogicalDeviceConfiguration(memory_limit=memory_mb)])
        else:
            tf.config.experimental.set_memory_growth(gpu, True)

    _TENSORFLOW_CONFIGURED = True


def export_yolo_for_inference(source, destination, metadata=None):
    """Export stock inference classes and portable input metadata.

    Preserve original training checkpoints, including historical segmentation
    archives, while removing custom criterion/class dependencies from exports.
    """
    import copy
    import torch
    from ultralytics.nn.tasks import SegmentationModel, DetectionModel
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError('Inference export must be a new file, preserving the training checkpoint.')
    checkpoint = torch.load(source, map_location='cpu', weights_only=False)
    original = checkpoint.get('ema') if checkpoint.get('ema') is not None else checkpoint.get('model')
    if not isinstance(original, DetectionModel):
        raise ValueError('An Ultralytics checkpoint is required for inference export.')
    model = copy.deepcopy(original)
    model.__class__ = SegmentationModel if isinstance(original, SegmentationModel) else DetectionModel
    if metadata:
        model.tempo_metadata = metadata
    model.__dict__.pop('criterion', None)
    model.cpu().eval()
    checkpoint.update(model=model, ema=None, optimizer=None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    torch.save(checkpoint, destination)
    return str(destination)


def unpack_stardist(path, folder):
    with zipfile.ZipFile(path) as bundle:
        for name in bundle.namelist():
            destination = (Path(folder)/name).resolve()
            if not destination.is_relative_to(Path(folder).resolve()):
                raise ValueError('Invalid model archive path.')
        bundle.extractall(folder)
    from stardist.models import StarDist2D  # type: ignore
    return StarDist2D(None, name='model', basedir=folder)


def train_native(model_type, dataset, output, epochs, batch, device, tf_memory_mb=None, *, validation_split='val'):
    from tempo_progress import report as progress
    progress(model_type, 'training', total=epochs)
    if model_type == 'cellpose':
        from tempo_cellpose import train_specialists
        train_x, train_y = load_cellpose_partition(dataset, 'train')
        val_x, val_y = load_cellpose_partition(dataset, validation_split)
        info = json.loads((Path(dataset)/'dataset.json').read_text()) if (Path(dataset)/'dataset.json').is_file() else {}
        helper = None
        if info.get('assisted', False):
            from tempo_nuclear_training import load_helpers, helper_directory
            helper_info = json.loads((helper_directory(dataset)/'helper_model.json').read_text())
            helper = dict(train=load_helpers(dataset,'train'), val=load_helpers(dataset,validation_split),
                          path=helper_info['path'], sha256=helper_info['sha256'])
        return train_specialists(train_x, train_y, val_x, val_y, output, epochs, batch, device,
                                 helper=helper, profile=info.get('preprocessing',{}).get('profile'), strict=bool(info), seed=info.get('seed',17))
    if model_type == 'stardist':
        from tempo_nuclear_training import train_nuclear_model
        return train_nuclear_model(dataset, output, epochs, batch, tf_memory_mb, validation_split=validation_split)
    raise ValueError(f'Unsupported native architecture: {model_type}')


def instance_counts(truth, predicted):
    """One-to-one instance matches at IoU >= 0.5; no dependence on instance IDs."""
    from scipy.optimize import linear_sum_assignment
    truth, predicted = np.asarray(truth), np.asarray(predicted)
    if truth.shape != predicted.shape:
        raise ValueError('Annotation and prediction masks must have identical dimensions.')
    gt_ids, gt_index, gt_area = np.unique(truth, return_inverse=True, return_counts=True)
    pred_ids, pred_index, pred_area = np.unique(predicted, return_inverse=True, return_counts=True)
    gt_positive, pred_positive = gt_ids > 0, pred_ids > 0
    gt_mapping = np.full(len(gt_ids), -1, dtype=np.int64)
    pred_mapping = np.full(len(pred_ids), -1, dtype=np.int64)
    gt_ids, pred_ids = gt_ids[gt_positive], pred_ids[pred_positive]
    gt_mapping[gt_positive] = np.arange(len(gt_ids))
    pred_mapping[pred_positive] = np.arange(len(pred_ids))
    if not len(gt_ids) or not len(pred_ids):
        return 0, len(pred_ids), len(gt_ids)
    # Count each intersecting label pair once instead of scanning the image
    # for every pair. Areas and matching weights retain the exact IoU formula.
    gt_index, pred_index = gt_mapping[gt_index.ravel()], pred_mapping[pred_index.ravel()]
    foreground = (gt_index >= 0) & (pred_index >= 0)
    intersection = np.bincount(gt_index[foreground] * len(pred_ids) + pred_index[foreground],
                               minlength=len(gt_ids)*len(pred_ids)).reshape(len(gt_ids), len(pred_ids))
    union = gt_area[gt_positive, None] + pred_area[None, pred_positive] - intersection
    overlap = intersection / union
    rows,cols = linear_sum_assignment(-(overlap>=0.5).astype(float)-overlap*1e-6)
    tp = sum(overlap[i,j]>=0.5 for i,j in zip(rows,cols))
    return int(tp), len(pred_ids)-int(tp), len(gt_ids)-int(tp)


def benchmark(model_type, path, dataset, device, *, split='test', helper_mode='normal', evidence_dir=None):
    if model_type == 'nano_scout':
        from tempo_evaluation_metrics import benchmark_scout
        return benchmark_scout(path, dataset, split, device)
    if model_type == 'cellpose':
        from tempo_cellpose import predict_batch, instance_image
        images, class_labels = load_cellpose_partition(dataset, split)
        originals=sorted((Path(dataset)/'raw/images'/split).glob('*.png'))
        processed=not originals
        if originals:
            if len(originals)!=len(images):raise ValueError('Raw evaluation images are incomplete.')
            images=[read_image_plane(image) for image in originals]
        predictions = predict_batch(path, images, device, processed=processed,
            **({'helper_mode':helper_mode} if helper_mode != 'normal' else {}),
            **({'cue_output':evidence_dir} if evidence_dir is not None else {}))
        dataset_info = json.loads((Path(dataset)/'dataset.json').read_text()) if (Path(dataset)/'dataset.json').is_file() else {}
        rows = []
        if not images or len(predictions) != len(images):
            raise ValueError('Every Cellpose evaluation image must have a prediction result.')
        active_classes = {name: cid for name, cid in MODEL_CLASSES.items() if cid < 2
                          or any(np.any(mask > 0) for mask in class_labels.get(name, []))
                          or any(instance.class_id == cid for instances in predictions for instance in instances)}
        counts = {name: [0, 0, 0] for name in active_classes}
        unknown = 0
        segmentation = [0, 0, 0]
        for index, (image, instances) in enumerate(zip(images, predictions)):
            per_counts = {}
            if evidence_dir is not None:
                folder=Path(evidence_dir);folder.mkdir(parents=True,exist_ok=True)
                preview=cv2.cvtColor(image.astype(np.uint8),cv2.COLOR_GRAY2BGR)
                for instance in instances:
                    contours,_=cv2.findContours(instance.mask.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                    row,column=instance.bounds
                    contours=[contour+np.array([[[column.start,row.start]]]) for contour in contours]
                    color=(0,200,255) if instance.class_id is None else (40,220,40) if instance.class_id%2==0 else (60,60,240)
                    cv2.drawContours(preview,contours,-1,color,1)
                    if instance.class_id is not None and instance.class_id>=2:
                        cv2.putText(preview,'G',(column.start,row.start+10),cv2.FONT_HERSHEY_SIMPLEX,.4,color,1)
                if not cv2.imwrite(str(folder/(str(index)+'.png')),preview):raise OSError('Could not save prediction preview.')
            unknown += sum(instance.class_id is None for instance in instances)
            truth = np.zeros(image.shape, np.int32)
            for name, cid in active_classes.items():
                target = class_labels[name][index]
                matched = instance_counts(target, instance_image(instances, image.shape, cid))
                counts[name] = [a + b for a, b in zip(counts[name], matched)]
                per_counts[name] = 2*matched[0]/max(1,2*matched[0]+matched[1]+matched[2]) if sum(matched) else None
                foreground = target > 0
                offset = int(truth.max())
                truth[foreground] = target[foreground] + offset
            matched = instance_counts(truth, instance_image(instances, image.shape))
            segmentation = [a + b for a, b in zip(segmentation, matched)]
            from tempo_evaluation_metrics import split_merge_rates
            splits, merges = split_merge_rates(truth, instance_image(instances,image.shape))
            record = next((r for r in dataset_info.get('records',[]) if r['stem'] == sorted((Path(dataset)/'cells/images'/split).glob('*.png'))[index].stem),dict(name=str(index),preparation=str(index)))
            from tempo_evaluation_metrics import subset_scores
            subsets=subset_scores(record.get('subset_shapes',[]),image.shape,instances)
            if evidence_dir is not None:
                if not cv2.imwrite(str(folder/(str(index)+'-source.png')),image.astype(np.uint8)):raise OSError('Could not save source preview.')
                reference=cv2.cvtColor(image.astype(np.uint8),cv2.COLOR_GRAY2BGR)
                for name,cid in active_classes.items():
                    target=class_labels[name][index]
                    for value in np.unique(target[target>0]):
                        contours,_=cv2.findContours((target==value).astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
                        cv2.drawContours(reference,contours,-1,(40,220,40) if cid%2==0 else (60,60,240),1)
                        if cid>=2:
                            x,y,_,_=cv2.boundingRect(contours[0]);cv2.putText(reference,'G',(x,y+10),cv2.FONT_HERSHEY_SIMPLEX,.4,(40,220,40) if cid%2==0 else (60,60,240),1)
                if not cv2.imwrite(str(folder/(str(index)+'-reference.png')),reference):raise OSError('Could not save reference preview.')
            rows.append(dict(name=record['name'],preparation=record['preparation'],splits=splits,merges=merges,
                             evidence_index=index,**subsets,**per_counts))
        tp, fp, fn = (sum(value[axis] for value in counts.values()) for axis in range(3))
        fp += unknown  # An unclassified prediction cannot count as correct viability.
        seg_tp, seg_fp, seg_fn = segmentation
        return {'metric': 'object_class_F1_IoU50' if any(np.any(mask > 0) for name,cid in MODEL_CLASSES.items() if cid >= 2 for mask in class_labels.get(name, [])) else 'cell_class_F1_IoU50', 'score': 2 * tp / max(1, 2 * tp + fp + fn),
                'class_scores': {name: 2 * a / max(1, 2 * a + b + c) for name, (a, b, c) in counts.items()},
                'precision':tp/max(1,tp+fp),'recall':tp/max(1,tp+fn),
                'matches':dict(tp=tp,fp=fp,fn=fn),
                'unclassified_predictions': unknown, 'per_image':rows,
                'segmentation_score': 2 * seg_tp / max(1, 2 * seg_tp + seg_fp + seg_fn)}
    if model_type == 'stardist':
        from tempo_nuclear_training import benchmark_nuclei
        return benchmark_nuclei(path, dataset, split)
    raise ValueError(f'Unsupported architecture for evaluation: {model_type}')



def publish_champions(base_dir, repo_id, token):
    """Archive a complete run and atomically install verified improvements."""
    import math
    from huggingface_hub import HfApi, CommitOperationAdd, hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError
    from tempo_hub import commit_and_confirm, _model_records
    root = Path(base_dir)
    report_path = root/'runs/training_report.json'
    report = json.loads(report_path.read_text())
    if (not report.get('success') or report.get('origin') != 'approved-master'
            or set(report.get('results', {})) != set(MODEL_FILES)):
        raise ValueError('A successful, complete model report is required.')
    if report.get('approval_policy') != APPROVAL_POLICY:
        raise ValueError('Retrain with explicitly approved annotations before saving these models.')
    if report.get('purpose') == 'training-check' or report.get('split_policy') != SPLIT_POLICY:
        raise ValueError('A complete training run is required before saving models.')
    if any(result.get('publishable') is False for result in report['results'].values()):
        raise ValueError('Training-check candidates cannot be published.')
    if 'remote_baselines' not in report:
        raise ValueError('Verify the current models before starting training.')
    run_id = report['run_id']
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', run_id):
        raise ValueError('Invalid training run ID.')
    api = HfApi(token=token)
    head = api.repo_info(repo_id=repo_id, repo_type='model').sha
    try:
        manifest = json.loads(Path(hf_hub_download(repo_id, 'model_manifest.json', revision=head, token=token)).read_text())
    except EntryNotFoundError:
        manifest = {'models': {}}
    manifest['models'] = _model_records(dict(manifest, revision=head))
    operations, outcomes = [], {}
    pair_replace = None
    from tempo_evaluation_metrics import validation_allows
    if 'cellpose' in report['results'] and 'stardist' in report['results']:
        from tempo_cellpose import bundle_metadata
        cell_result = report['results']['cellpose']
        dependency = bundle_metadata(cell_result['artifact']).get('helper_sha256')
        if dependency:
            if dependency != report['results']['stardist']['sha256']:
                raise ValueError('Cellpose was evaluated with a different nuclear helper.')
            incumbent = manifest['models'].get(MODEL_FILES['cellpose'])
            previous = cell_result.get('baseline_metrics')
            pair_replace = incumbent is None or (report.get('evaluation_partition') == 'test' and previous
                            and cell_result['metrics']['score'] > previous['score'])
            if (incumbent or {}).get('input_format') != DATASET_FORMAT and not validation_allows(report.get('architecture_validation'), bundle_metadata(cell_result['artifact'])['preprocessing']['profile'], 'cellpose'):
                pair_replace = False
    prefix = f'runs/{run_id}'
    updated = 0
    for model_type, result in report['results'].items():
        name = MODEL_FILES[model_type]
        path = Path(result['artifact'])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != result['sha256']:
            raise ValueError('Trained weights changed after verification.')
        metrics = result['metrics']
        if not math.isfinite(metrics['score']):
            raise ValueError('Model evaluation returned a non-finite score.')
        current = manifest['models'].get(name)
        if (current or {}).get('sha256') != report['remote_baselines'].get(name):
            raise ValueError('Current models changed since comparison. Evaluate them again before replacement.')
        previous = result.get('baseline_metrics')
        if current is not None:
            if (not previous or not math.isfinite(previous['score'])
                    or previous.get('metric') != metrics.get('metric')):
                raise ValueError('The current model comparison is missing or invalid.')
            replace = report.get('evaluation_partition', 'test') == 'test' and metrics['score'] > previous['score']
            outcome = 'Updated' if replace else 'Kept current model'
        else:
            replace, outcome = True, 'First model saved'
        if model_type == 'nano_scout' and (current or {}).get('input_format') != DATASET_FORMAT:
            # Real centroid exports carry metadata; legacy transaction fixtures do not.
            if result.get('scout_metadata') and not validation_allows(report.get('architecture_validation'), result['scout_metadata']['preprocessing']['profile'], 'nano_scout', result['scout_metadata']['box_width']):
                replace=False
                outcome='Awaiting experiment validation'
        if pair_replace is not None and model_type in ('stardist','cellpose'):
            replace = bool(pair_replace)
            outcome = 'Updated together' if replace else 'Kept coupled models'
        if replace:
            manifest['models'][name] = dict(sha256=digest, version=run_id, architecture=model_type, input_format=DATASET_FORMAT,
                                          helper_sha256=(report['results']['stardist']['sha256'] if pair_replace is not None and model_type=='cellpose' else None),
                                          metrics=metrics, test_fingerprint=report['test_fingerprint'],
                                          evaluation_basis=report.get('evaluation_basis', 'held-out-images'))
            operations.append(CommitOperationAdd(path_in_repo=name, path_or_fileobj=str(path)))
            updated += 1
        outcomes[model_type] = dict(outcome=outcome, file=name, metrics=metrics,
                                    baseline_metrics=previous, current_sha256=manifest['models'].get(name,{}).get('sha256'))
        operations.append(CommitOperationAdd(path_in_repo=f'{prefix}/weights/{name}', path_or_fileobj=str(path)))
    if pair_replace:
        manifest['models'].pop('yolo26s-seg.pt',None)
        manifest['models'].pop('stardist_cardio.zip',None)
    report.update(model_outcomes=outcomes, updated_count=updated, archive_prefix=prefix,
                  current_models=manifest['models'])
    operations.append(CommitOperationAdd(path_in_repo=f'{prefix}/report.json',
                                         path_or_fileobj=json.dumps(report, indent=2, allow_nan=False).encode()))
    operations.append(CommitOperationAdd(path_in_repo=f'{prefix}/training_splits.json',
                                         path_or_fileobj=str(root/'datasets/training_splits.json')))
    if pair_replace:
        manifest['models'].pop('yolo26s-seg.pt',None)
        manifest['models'].pop('stardist_cardio.zip',None)
        report['current_models']=manifest['models']
    if updated:
        manifest.update(approval_policy=APPROVAL_POLICY, split_policy=SPLIT_POLICY)
        manifest.pop('revision', None)
        operations.append(CommitOperationAdd(path_in_repo='model_manifest.json',
                                             path_or_fileobj=json.dumps(manifest, indent=2, allow_nan=False).encode()))
    result = commit_and_confirm(api, repo_id=repo_id, repo_type='model', token=token,
                                parent_commit=head, operations=operations,
                                commit_message='Save TEMPO training results and model improvements')
    report['publication_revision'] = getattr(result, 'oid', None) or api.repo_info(repo_id=repo_id, repo_type='model').sha
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False))
    return updated


def deploy_local_champions(base_dir):
    """Stage and roll back a complete local update, retaining coupled helpers."""
    from tempo_cellpose import bundle_metadata
    from tempo_evaluation_metrics import validation_allows
    root=Path(base_dir)
    report=json.loads((root/'runs/training_report.json').read_text())
    if not report.get('success') or report.get('origin')!='approved-master' or report.get('approval_policy')!=APPROVAL_POLICY:
        raise ValueError('A complete approved-master training report is required.')
    results=report['results'];selected={role for role,result in results.items() if result.get('promote')}
    cell=results.get('cellpose')
    if cell:
        meta=bundle_metadata(cell['artifact'])
        if meta.get('helper_sha256'):
            if meta['helper_sha256']!=results.get('stardist',{}).get('sha256'):
                raise ValueError('The local Cellpose nuclear dependency differs.')
            if 'cellpose' in selected and validation_allows(report.get('architecture_validation'),meta['preprocessing']['profile'],'cellpose'):
                selected.add('stardist')
            else:selected-= {'cellpose','stardist'}
    scout=results.get('nano_scout',{}).get('scout_metadata')
    if scout and not validation_allows(report.get('architecture_validation'),scout['preprocessing']['profile'],'nano_scout',scout['box_width']):
        selected.discard('nano_scout')
    folder=Path(tempfile.mkdtemp(prefix='.model-update-',dir=root))
    retain_backups=False
    try:
        staged={};backups={};attempted=[]
        for role in sorted(selected):
            result=results[role];source=Path(result['artifact']);name=MODEL_FILES[role];target=root/name
            if hashlib.sha256(source.read_bytes()).hexdigest()!=result['sha256']:
                raise ValueError('Candidate weights changed after evaluation.')
            current=hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
            if current!=result.get('baseline_sha256'):
                raise ValueError('An installed model changed after comparison.')
            staged[name]=folder/name;shutil.copy2(source,staged[name])
            if target.exists():backups[name]=folder/(name+'.previous');shutil.copy2(target,backups[name])
        try:
            for name,path in staged.items():
                attempted.append(name);os.replace(path,root/name)
        except BaseException as exc:
            rollback_errors=[]
            for name in reversed(attempted):
                try:
                    if name in backups:os.replace(backups[name],root/name)
                    else:(root/name).unlink(missing_ok=True)
                except OSError as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                retain_backups=True
                raise RuntimeError(f'Model update failed and restoration needs attention. Backups are retained in {folder}. '+ '; '.join(rollback_errors)) from exc
            raise
    finally:
        if not retain_backups:
            shutil.rmtree(folder)
    return sorted(selected)
