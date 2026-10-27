"""Layer-aware annotation proposals; scout estimates never become masks."""
import copy
import cv2
import numpy as np
from tempo_nuclei import is_nucleus


def polygon_iou(left, right, width, height):
    polygons = [np.asarray(shape['polygon'],np.float64) for shape in (left,right)]
    points = np.concatenate(polygons)
    x0,y0 = np.maximum(0,np.floor(points.min(axis=0))).astype(int)
    x1,y1 = np.minimum([width-1,height-1],np.ceil(points.max(axis=0))).astype(int)
    if x1 < x0 or y1 < y0:
        return 0.
    masks = []
    for polygon in polygons:
        mask = np.zeros((y1-y0+1,x1-x0+1),np.uint8)
        cv2.fillPoly(mask,[np.rint(polygon-[x0,y0]).astype(np.int32)],1)
        masks.append(mask.astype(bool))
    return np.count_nonzero(masks[0]&masks[1])/max(1,np.count_nonzero(masks[0]|masks[1]))


def merge_nuclear_proposals(existing, proposed, width, height):
    result = copy.deepcopy(existing)
    nuclei = [shape for shape in result if is_nucleus(shape)]
    for shape in proposed:
        if not is_nucleus(shape):
            raise ValueError('StarDist returned a non-nuclear annotation.')
        if not any(polygon_iou(shape,other,width,height)>=.5 for other in nuclei):
            value = copy.deepcopy(shape)
            value['proposal'] = True
            result.append(value); nuclei.append(value)
    return result
