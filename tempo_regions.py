"""Cell/group identity shared by annotations, model labels and measurements."""
from numbers import Integral

MODEL_CLASSES = {'healthy': 0, 'dead': 1, 'healthy_group': 2, 'dead_group': 3}
GROUP_CLASSES = {name: value for name, value in MODEL_CLASSES.items() if value >= 2}


def object_type(record):
    kind = record.get('object_type', 'cell')
    if kind not in ('cell', 'group', 'nucleus'):
        raise ValueError('Object type must be cell, group or nucleus.')
    if kind == 'group' and record.get('class_id') == 2:
        raise ValueError('A nucleus cannot be marked as a group.')
    return kind


def model_class(record):
    """Nuclei have no scout/Cellpose class; group model labels are separate."""
    kind = object_type(record)
    cid = record.get('class_id')
    if isinstance(cid, bool) or not isinstance(cid, Integral) or cid not in (0, 1, 2):
        raise ValueError('Unknown annotation class.')
    if (cid == 2) != (kind == 'nucleus'):
        raise ValueError('A nucleus requires its explicit annotation kind and class.')
    return None if cid == 2 else int(cid) + (2 if kind == 'group' else 0)


def annotation_class(cid):
    if isinstance(cid, bool) or not isinstance(cid, Integral) or cid not in MODEL_CLASSES.values():
        raise ValueError('Unknown model class.')
    result = {'class_id': int(cid) % 2}
    if cid >= 2:
        result['object_type'] = 'group'
    return result


def region_counts(boxes):
    counts = {'live_count': 0, 'dead_count': 0, 'healthy_groups': 0, 'dead_groups': 0}
    for box in boxes:
        if box.get('class_id') not in (0, 1) or box.get('requires_viability_review'):
            continue
        key = ('healthy_groups' if box['class_id'] == 0 else 'dead_groups') if object_type(box) == 'group' else (
            'live_count' if box['class_id'] == 0 else 'dead_count')
        counts[key] += 1
    return counts


def scout_qualifies(counts, threshold):
    return counts['live_count'] >= threshold or counts['healthy_groups'] > 0
