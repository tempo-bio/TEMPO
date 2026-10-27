"""Statistics for an unoriented image-plane axis (period 180 degrees)."""
import numpy as np


def axial_statistics(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return float('nan'), float('nan')
    resultant = np.mean(np.exp(2j * np.deg2rad(values)))
    length = min(1.0, abs(resultant))
    if length < 1e-12:
        return float('nan'), float('nan')
    mean = float(np.rad2deg(np.angle(resultant)) / 2 % 180)
    if np.isclose(mean, 180):
        mean = 0.0
    spread = float(np.rad2deg(np.sqrt(-2 * np.log(length))) / 2)
    return mean, spread
