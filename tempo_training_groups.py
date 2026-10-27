"""Persistent image-field assignments for approved training data."""
import hashlib

SPLIT_POLICY = 'image-field-v1'
PARTITIONS = ('train', 'val', 'test')


def validate_assignments(names, assignments):
    if not names:
        raise ValueError('No approved images are available.')
    if not isinstance(assignments, dict) or set(assignments) != set(names):
        raise ValueError('Training assignments do not match the approved images.')
    if any(value not in PARTITIONS for value in assignments.values()):
        raise ValueError('Invalid saved training assignment.')
    if 'train' not in assignments.values():
        raise ValueError('No training images are available. Approve additional images before training.')
    return dict(assignments)


def plan_splits(names, groups=None, previous=None, previous_groups=None):
    """Preserve image roles, including training images already seen by a model."""
    if not names:
        raise ValueError('No approved images are available.')
    previous = previous or {}
    if not isinstance(previous, dict) or any(value not in PARTITIONS for value in previous.values()):
        raise ValueError('Invalid saved training assignments.')
    if groups:
        if set(groups) != set(names) or any(not isinstance(value,str) or not value for value in groups.values()):
            raise ValueError('Every image needs a biological preparation/animal identifier.')
        old_groups = {}
        for name, partition in previous.items():
            if name not in groups:
                continue
            group = groups[name]
            if group in old_groups and old_groups[group] != partition:
                raise ValueError('Previously exposed fields from one preparation cross partitions. Use a new independent experiment cohort.')
            old_groups[group] = partition
        if previous_groups and any(name in previous_groups and previous_groups[name] != group for name,group in groups.items()):
            raise ValueError('A biological preparation identity changed; resolve it before training.')
        assigned = plan_splits(set(groups.values()),previous=old_groups)
        return validate_assignments(names,{name:assigned[group] for name,group in groups.items()})
    ordered = sorted(set(names), key=lambda name: hashlib.sha256(name.encode()).hexdigest())
    assignments = {name: previous[name] for name in ordered if name in previous}
    new = [name for name in ordered if name not in assignments]
    if not assignments:
        if len(ordered) == 1:
            assignments[ordered[0]] = 'train'
        elif len(ordered) == 2:
            assignments.update({ordered[0]: 'test', ordered[1]: 'train'})
        else:
            count = max(1, int(len(ordered) * .15))
            assignments.update({name: 'test' if index < count else 'val' if index < 2*count else 'train'
                                for index, name in enumerate(ordered)})
    else:
        desired = ['train'] + (['test'] if len(ordered) >= 2 else []) + (['val'] if len(ordered) >= 3 else [])
        for partition in desired:
            if partition not in assignments.values() and new:
                assignments[new.pop(0)] = partition
        for name in new:
            bucket = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 100
            assignments[name] = 'test' if bucket < 15 else 'val' if bucket < 30 else 'train'
    return validate_assignments(names, assignments)


def partition_settings(assignments):
    return dict(validation_partition='val' if 'val' in assignments.values() else 'train',
                evaluation_partition='test' if 'test' in assignments.values() else 'train',
                evaluation_basis='held-out-images' if 'test' in assignments.values() else 'training-images')
