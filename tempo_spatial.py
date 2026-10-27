"""Optional, calibrated image-to-well registration; absent metadata stays unknown.

An input video may have a sibling <video filename>.spatial.json. Processing
copies the validated document into its exact output directory. Coordinates are
micrometres in one named well coordinate system, never inferred from field IDs.
"""
import json
from pathlib import Path
import numpy as np

SPATIAL_VERSION = 1


def validate_registration(document):
    if not isinstance(document, dict) or type(document.get('version')) is not int or document.get('version') != SPATIAL_VERSION:
        raise ValueError('Unsupported spatial metadata version.')
    if document.get('units') != 'um':
        raise ValueError('Spatial registration units must be um.')
    for key in ('recording_id', 'well_id', 'field_id', 'coordinate_system'):
        if not isinstance(document.get(key), str) or not document[key].strip():
            raise ValueError(f'Spatial metadata needs {key}.')
    size = document.get('image_size_px')
    if not isinstance(size, list) or len(size) != 2 or any(
            isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in size):
        raise ValueError('Image size must contain positive integer width and height.')
    matrix = np.asarray(document.get('pixel_to_well'), dtype=float)
    if matrix.shape != (2, 3) or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix[:, :2])) < 1e-12:
        raise ValueError('Image-to-well transform must be a finite, nonsingular 2 by 3 affine matrix.')
    geometry = document.get('well_geometry')
    if geometry is not None:
        if not isinstance(geometry, dict):
            raise ValueError('Well geometry must describe a circle.')
        circle = np.asarray([geometry.get('center_x'), geometry.get('center_y'), geometry.get('radius')], dtype=float)
        if not np.isfinite(circle).all() or circle[2] <= 0:
            raise ValueError('Well geometry needs a finite centre and positive radius in um.')
    # Round-trip strips foreign object types and rejects non-JSON numeric values.
    return json.loads(json.dumps(document, allow_nan=False))


def read_registration(path):
    try:
        return validate_registration(json.loads(Path(path).read_text()))
    except (TypeError, OverflowError) as exc:
        raise ValueError('Malformed spatial metadata.') from exc


def registration_for_row(dataset_path, row):
    relative = row.get('Spatial_Metadata_File')
    if not isinstance(relative, str) or not relative:
        return None
    base = Path(dataset_path).resolve().parent
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('Spatial metadata must remain within the results directory.')
    document = read_registration(path)
    if document['recording_id'] != str(row['Recording_ID']) or document['well_id'] != str(row['Well_ID']) or document['field_id'] != str(row['FOV_ID']):
        raise ValueError('Spatial metadata does not identify this recording, well and field.')
    return document


def transform_points(document, x, y):
    matrix = np.asarray(document['pixel_to_well'], dtype=float)
    points = np.column_stack((x, y, np.ones(len(x))))
    return points @ matrix.T


def field_outline(document):
    width, height = document['image_size_px']
    return transform_points(document, [0, width, width, 0, 0], [0, 0, height, height, 0])
