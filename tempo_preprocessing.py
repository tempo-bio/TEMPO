"""Versioned microscopy inputs shared by training, previews and inference."""
import cv2
import numpy as np

VERSION = 1
PROFILES = ('P0', 'P1', 'P2', 'clahe4', 'gaussian1', 'gaussian1-clahe2')


def metadata(profile='P0'):
    if profile not in PROFILES:
        raise ValueError('Unknown preprocessing recipe: '+str(profile))
    if profile in ('clahe4', 'gaussian1', 'gaussian1-clahe2'):
        return dict(version=2, profile=profile, percentile=[1., 99.8],
                    clip_limit=4. if profile=='clahe4' else 2. if profile=='gaussian1-clahe2' else None,
                    tile_grid=[8, 8], gaussian_sigma=0. if profile=='clahe4' else 1.,
                    gaussian_kernel=[7, 7], border='reflect101', spatial_units='source-image pixels',
                    order=['percentile_normalization']+(['gaussian'] if profile!='clahe4' else [])+
                          (['clahe'] if profile!='gaussian1' else []),
                    clahe_encoding='uint8' if profile!='gaussian1' else None)
    return dict(version=VERSION, profile=profile, percentile=[1., 99.8],
                clip_limit=2., tile_grid=[8, 8], median_size=3)


def preprocess(image, profile='P0'):
    settings = metadata(profile)
    plane = np.asarray(image)
    if plane.ndim == 3:
        plane = plane[..., 0] if plane.shape[-1] == 1 else cv2.cvtColor(plane, cv2.COLOR_BGR2GRAY)
    if plane.ndim != 2 or not plane.size or not np.isfinite(plane).all():
        raise ValueError('A finite two-dimensional microscopy image is required.')
    low, high = np.percentile(plane, settings['percentile'])
    if high <= low:
        return np.zeros(plane.shape, np.float32)
    result = np.clip((plane.astype(np.float32) - low) / (high - low), 0, 1)
    if settings.get('gaussian_sigma', 0):
        result = cv2.GaussianBlur(result.astype(np.float32), (7, 7), 1., borderType=cv2.BORDER_REFLECT_101)
    if profile not in ('P0', 'gaussian1'):
        encoded = np.rint(result * 255).astype(np.uint8)
        if profile == 'P2':
            encoded = cv2.medianBlur(encoded, 3)
        result = cv2.createCLAHE(clipLimit=settings['clip_limit'], tileGridSize=(8, 8)).apply(encoded).astype(np.float32) / 255
    return result.astype(np.float32)


def preview_image(path, enhanced=False):
    """Return a detached QImage, keeping source coordinates and raw files intact."""
    from PyQt6.QtGui import QImage
    if not enhanced:
        return QImage(str(path))
    plane = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
    if plane is None:
        raise ValueError('Cannot read preview image.')
    pixels = np.ascontiguousarray(np.rint(preprocess(plane, 'P1') * 255).astype(np.uint8))
    return QImage(pixels.data, pixels.shape[1], pixels.shape[0], pixels.strides[0],
                  QImage.Format.Format_Grayscale8).copy()
