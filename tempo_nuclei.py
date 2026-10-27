"""Partial nuclear supervision. Nuclear multiplicity never defines cell identity."""
import copy
import hashlib
import json
import uuid
import cv2
import numpy as np

ANNOTATION_VERSION = 3
SUPPLEMENT_FORMAT = 'tempo-nuclei-v1'


def is_nucleus(shape):
    return shape.get('object_type') == 'nucleus'


def migrate_record(record):
    """Drop legacy class 2; only explicitly typed nuclei can use the new slot."""
    if not isinstance(record,dict) or not isinstance(record.get('boxes',[]),list) or any(not isinstance(shape,dict) for shape in record.get('boxes',[])):
        raise ValueError('Expected an annotation record and object list.')
    result = copy.deepcopy(record)
    if result.get('annotation_version', ANNOTATION_VERSION) > ANNOTATION_VERSION:
        raise ValueError('Update TEMPO to read this annotation format.')
    result['boxes'] = [shape for shape in result.get('boxes', [])
                       if shape.get('class_id') != 2 or is_nucleus(shape)]
    result['annotation_version'] = ANNOTATION_VERSION
    return result


def cell_digest(record):
    cells = [dict(class_id=shape['class_id'],object_type=shape.get('object_type','cell'),polygon=shape['polygon'])
             for shape in migrate_record(record)['boxes'] if not is_nucleus(shape)]
    return hashlib.sha256(json.dumps(dict(boxes=cells, width=record.get('width'), height=record.get('height'),
                                        coordinate_space=record.get('coordinate_space')), sort_keys=True).encode()).hexdigest()


def supplement(record, *, status='draft'):
    if not record.get('image_id'):
        raise ValueError('Nuclear supplements require a verified image identity.')
    nuclei = copy.deepcopy([shape for shape in record.get('boxes', []) if is_nucleus(shape)])
    for shape in nuclei:
        shape.setdefault('annotation_id', uuid.uuid4().hex)
    return dict(format=SUPPLEMENT_FORMAT, image_id=record['image_id'],
                source_sha256=record['image_id'].removeprefix('sha256:'),
                cell_sha256=cell_digest(record), nuclei=nuclei, status=status,
                width=record['width'], height=record['height'], coordinate_space=record['coordinate_space'])


def attach_supplement(record, nuclear):
    result = migrate_record(record)
    if not nuclear:
        return result
    if (nuclear.get('format') != SUPPLEMENT_FORMAT or nuclear.get('image_id') != result.get('image_id')
            or nuclear.get('source_sha256') != result.get('image_id', '').removeprefix('sha256:')):
        raise ValueError('Nuclear annotations refer to a different image or unsupported format.')
    if (nuclear.get('width'), nuclear.get('height'), nuclear.get('coordinate_space')) != (
            result.get('width'), result.get('height'), result.get('coordinate_space')):
        raise ValueError('Nuclear and cell annotation coordinates differ.')
    result['boxes'] = [shape for shape in result['boxes'] if not is_nucleus(shape)] + copy.deepcopy(nuclear['nuclei'])
    result['nuclear_status'] = nuclear['status'] if nuclear['cell_sha256'] == cell_digest(record) else 'needs_review'
    return result


def raster_polygon(shape, size):
    h, w = size
    points = np.rint(np.asarray(shape['polygon']) * [w - 1, h - 1]).astype(np.int32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [points], 1)
    return mask


def nuclear_labels(record, size, boundary=2):
    """Outside all cells is negative; unlabelled cell interiors stay unknown."""
    support = np.zeros(size, np.uint8)
    for shape in record['boxes']:
        if not is_nucleus(shape) and shape.get('class_id') in (0, 1):
            support |= raster_polygon(shape, size)
    expanded = cv2.dilate(support, np.ones((2 * boundary + 1,) * 2, np.uint8))
    labels = np.where(expanded, -1, 0).astype(np.int32)
    if record.get('nuclear_status') != 'approved':
        return labels
    index = 0
    for shape in record['boxes']:
        if not is_nucleus(shape):
            continue
        mask = raster_polygon(shape, size).astype(bool)
        if np.any(mask & ~expanded.astype(bool)):
            raise ValueError('A nucleus extends beyond its cell support; review the outlines.')
        if np.any(mask & (labels > 0)):
            raise ValueError('Nuclear instance outlines overlap; review them.')
        index += 1
        labels[mask] = index
    return labels


def helper_channels(image, helper):
    plane, cue = np.asarray(image, np.float32), np.asarray(helper, np.float32)
    if plane.ndim != 2 or cue.shape != plane.shape or not np.isfinite(cue).all() or np.any((cue < 0) | (cue > 1)):
        raise ValueError('Nuclear helper must be finite, aligned and in [0, 1].')
    return np.stack((plane, cue, np.zeros_like(plane)), axis=-1)


def continuous_map(model, image):
    prediction = model.predict(image)
    cue = np.asarray(prediction[0], np.float32).squeeze()
    # StarDist samples grid points at source coordinates 0, grid, 2*grid, ...
    grid = getattr(model.config, 'grid', (2, 2))
    yy, xx = np.indices(image.shape[:2], dtype=np.float32)
    return np.clip(cv2.remap(cue, xx / grid[1], yy / grid[0], cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE), 0, 1)
