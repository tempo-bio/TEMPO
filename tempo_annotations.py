"""Shared annotation schema and validation for drafts, review and training."""
import copy
import math
import re
from collections import defaultdict
from numbers import Integral, Real
from tempo_regions import object_type
from tempo_nuclei import migrate_record, is_nucleus


def image_filename(name):
    """Dataset identities must never escape the image directory on any host."""
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or any(c in name for c in '/\\:<>"|?*') or name[-1] in '. '
            or any(ord(c) < 32 for c in name)
            or name.split('.')[0].upper() in {'CON','PRN','AUX','NUL'}
            or re.fullmatch(r'(COM|LPT)[1-9]', name.split('.')[0].upper())):
        raise ValueError('Invalid image filename.')
    return name


def canonical_image_name(manifest, name):
    image_filename(name)
    return manifest.get('aliases', {}).get(name, name)


def annotation_geometry(record):
    """Compare actual annotations without lease, sync or display metadata."""
    record = pixel_record(record)
    boxes = copy.deepcopy(record.get('boxes', []))
    for box in boxes:
        if 'polygon' not in box and 'box' in box:
            x, y, w, h = box.pop('box')
            box['polygon'] = [[x,y], [x+w,y], [x+w,y+h], [x,y+h]]
    return boxes


def merge_image_records(records, manifest):
    """Collapse proven aliases, preserving rich drafts and rejecting ambiguity."""
    if not isinstance(records, dict):
        raise ValueError('Expected named annotation records.')
    result = {}
    for name, record in records.items():
        canonical = canonical_image_name(manifest, name)
        if not isinstance(record, dict) or not isinstance(record.get('boxes', []), list):
            raise ValueError(f'{name}: invalid annotation record.')
        old = result.get(canonical)
        if old is None:
            result[canonical] = copy.deepcopy(record)
            continue
        if old.get('boxes') and record.get('boxes'):
            if annotation_geometry(old) != annotation_geometry(record):
                raise ValueError(f'{canonical}: aliases contain different annotations; '
                                 'both drafts were preserved. Resolve the conflict before continuing.')
            if (old.get('sync_pending') and record.get('sync_pending')
                    and old.get('draft_owner') != record.get('draft_owner')):
                raise ValueError(f'{canonical}: unsynced alias drafts have different owners.')
        # An empty downloaded alias must never replace annotations or an approval.
        prefer_new = (not old.get('boxes') and bool(record.get('boxes'))
                      or bool(record.get('boxes')) and record.get('status') == 'approved'
                      and old.get('status') != 'approved')
        chosen, other = (record, old) if prefer_new else (old, record)
        merged = copy.deepcopy(chosen)
        for key, value in other.items():
            if key not in merged:
                merged[key] = copy.deepcopy(value)
        if old.get('sync_pending') or record.get('sync_pending'):
            pending = old if old.get('sync_pending') else record
            merged['sync_pending'] = True
            if pending.get('draft_owner'):
                merged['draft_owner'] = pending['draft_owner']
        if old.get('status') == 'approved' or record.get('status') == 'approved':
            merged['status'] = 'approved'
        result[canonical] = merged
    return result


def reconcile_image_identities(manifest, master, image_hashes):
    """Build a content-verified index for historical names at one remote revision.

    Filename aliases are inferred only through the documented historical naming
    convention or original_filename metadata, and checked against served bytes.
    Canonical names favor existing master records, so annotation inputs stay put.
    """
    hashes = {}
    for name, digest in image_hashes.items():
        image_filename(name)
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError(f'{name}: missing verified image checksum.')
        hashes[name] = digest
    groups = defaultdict(set)
    resolved = dict(hashes)
    old_images = manifest.get('images', {})
    for name, record in {**old_images, **master}.items():
        image_filename(name)
        candidates = {name, record.get('original_filename'),
                      re.sub(r'^\d{8}_\d{6}_', '', name)}
        candidates.discard(None)
        available = {hashes[candidate] for candidate in candidates if candidate in hashes}
        if len(available) > 1:
            raise ValueError(f'{name}: historical names point to different image bytes.')
        if not available:
            raise ValueError(f'{name}: no image exists for its annotation or reservation record.')
        resolved[name] = next(iter(available))
    for name, digest in resolved.items():
        groups[digest].add(name)
    images, aliases = {}, {}
    for digest, names in groups.items():
        approved = sorted(name for name in names if master.get(name, {}).get('status') == 'approved')
        annotated = sorted(name for name in names if name in master)
        canonical = (approved or annotated or sorted(names))[0]
        for name in names:
            if name != canonical:
                aliases[name] = canonical
        records = [old_images[name] for name in sorted(names) if name in old_images]
        pending = [record for record in records if record.get('status') in ('locked', 'submitted')]
        if not approved and not any(record.get('status') == 'approved' for record in records):
            owners = {record.get('claimed_by') for record in pending}
            submissions = {record.get('submission_id') for record in pending if record.get('status') == 'submitted'}
            if len(owners) > 1 or len(submissions) > 1:
                raise ValueError(f'{canonical}: duplicate images have conflicting reservations or submissions.')
            entry = copy.deepcopy(next((record for record in pending if record.get('status') == 'submitted'),
                                       pending[0] if pending else {'status':'available'}))
        else:
            entry = copy.deepcopy(next((record for record in records if record.get('status') == 'approved'), {}))
            entry['status'] = 'approved'
            for key in ('claimed_by', 'claimed_at', 'expires_at'):
                entry.pop(key, None)
        served = canonical if canonical in hashes else sorted(name for name in names if name in hashes)[0]
        entry.update(image_id='sha256:'+digest, sha256=digest, remote_filename=served,
                     original_filename=master.get(canonical, {}).get('original_filename')
                     or re.sub(r'^\d{8}_\d{6}_', '', canonical))
        images[canonical] = entry
    result = dict(copy.deepcopy(manifest), version=2, images=images, aliases=aliases)
    merge_image_records(master, result)  # Never certify ambiguous annotation collapse.
    return result


def _dimension(value):
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError('Image dimensions must be positive integers.')
    return int(value)


def _finite_points(points):
    if not isinstance(points, (list, tuple)) or any(
            not isinstance(point, (list, tuple)) or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, Real)
                   or not math.isfinite(value) for value in point) for point in points):
        raise ValueError('Polygon coordinates must be finite numeric pairs.')
    return points


def validate_annotations(records, *, allow_unclassified=False):
    """Return counts; cloud records use normalized coordinates and explicit dimensions."""
    cells = frames = dead = 0
    if not isinstance(records, dict) or not records:
        raise ValueError('No annotated images selected.')
    for name, record in records.items():
        if not isinstance(name, str) or not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Invalid image filename.')
        if not isinstance(record, dict) or not isinstance(record.get('boxes', []), list):
            raise ValueError(f'{name}: expected an annotation record and a list of boxes.')
        if record.get('coordinate_space') != 'normalized':
            raise ValueError(f'{name}: expected normalized coordinates.')
        for key in ('width', 'height'):
            _dimension(record.get(key))
        frame_cells = 0
        for box in record.get('boxes', []):
            if not isinstance(box, dict):
                raise ValueError(f'{name}: expected an annotation object.')
            cid = box.get('class_id')
            object_type(box)
            if isinstance(cid, bool) or not isinstance(cid, Integral) or cid not in (0, 1, 2):
                raise ValueError(f'{name}: unknown class {cid}.')
            if (cid == 2) != is_nucleus(box):
                raise ValueError(f'{name}: unknown annotation kind; migrate legacy annotations first.')
            if box.get('requires_viability_review') and not allow_unclassified:
                raise ValueError(f'{name}: classify all unclassified cell outlines as healthy or dead before continuing.')
            pts = _finite_points(box.get('polygon', []))
            if len(pts) < 3 or any(not 0 <= v <= 1 for p in pts for v in p):
                raise ValueError(f'{name}: polygon needs at least three finite points in [0, 1].')
            area = abs(sum(p[0]*q[1]-q[0]*p[1] for p,q in zip(pts, pts[1:]+pts[:1]))) / 2
            if area <= 1e-12:
                raise ValueError(f'{name}: polygon has zero area.')
            if cid in (0, 1) and not box.get('requires_viability_review'):
                cells += 1
                frame_cells += 1
                dead += cid == 1
        frames += frame_cells > 0
    return cells, frames, dead


def normalize_record(record, width, height):
    width, height = _dimension(width), _dimension(height)
    if not isinstance(record, dict) or not isinstance(record.get('boxes', []), list):
        raise ValueError('Expected an annotation record and a list of boxes.')
    result = migrate_record(record)
    normalized = result.get('coordinate_space') == 'normalized'
    result.update(width=int(width), height=int(height), coordinate_space='normalized')
    for box in result.get('boxes', []):
        if not isinstance(box, dict):
            raise ValueError('Expected an annotation object.')
        pts = box.get('polygon')
        if not pts and 'box' in box:
            rectangle = box['box']
            if not isinstance(rectangle, (list, tuple)) or len(rectangle) != 4:
                raise ValueError('Legacy box requires four finite coordinates.')
            _finite_points([rectangle[:2], rectangle[2:]])
            x,y,w,h = rectangle
            pts = [[x,y],[x+w,y],[x+w,y+h],[x,y+h]]
        pts = _finite_points([] if pts is None else pts)
        if box.get('class_id') == -1 or box.get('dead'):
            box['class_id'] = 1
        box['polygon'] = [[min(1.0, max(0.0, float(p[0]))), min(1.0, max(0.0, float(p[1])))] for p in (pts or [])] if normalized else [[min(1.0, max(0.0, float(x)/width)), min(1.0, max(0.0, float(y)/height))] for x,y in (pts or [])]
        box.pop('box', None)
    return result


def pixel_record(record):
    result = copy.deepcopy(record)
    if result.get('coordinate_space') == 'normalized':
        for box in result.get('boxes', []):
            box['polygon'] = [[x*result['width'], y*result['height']] for x,y in box['polygon']]
        result['coordinate_space'] = 'pixels'
    return result
