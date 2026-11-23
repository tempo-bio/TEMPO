"""Fit and evaluate a TEMPO focus profile from labeled camera TIFFs."""

import argparse
import csv
import os
from pathlib import Path

import cv2

from tempo_focus_calibration import read_focus_reference, read_planes
from tempo_processing import atomic_write, file_lock
from tempo_quality import fit_profile


COLUMNS = ('image', 'field', 'partition', 'label')


def read_reference(path):
    settings = read_focus_reference(path)
    return settings.signature, settings.sensor_bits, settings.shape, settings.dtype


def read_examples(manifest, shape, dtype):
    records = []
    seen = set()
    with manifest.open(encoding='utf-8-sig', newline='') as stream:
        rows = csv.DictReader(stream, strict=True)
        if rows.fieldnames is None or sorted(rows.fieldnames) != sorted(COLUMNS):
            raise ValueError('The CSV needs the columns image,field,partition,label.')
        for row in rows:
            if None in row or any(not row[key] or not row[key].strip() for key in COLUMNS):
                raise ValueError(f'{manifest.name}, line {rows.line_num}: fill in all four columns.')
            example = {key: row[key].strip() for key in COLUMNS}
            location = f"{manifest.name}, line {rows.line_num} ({example['image']})"
            if example['partition'] not in ('train', 'test') or example['label'] not in ('sharp', 'out_of_focus'):
                raise ValueError(f'{location}: use train or test, and sharp or out_of_focus.')
            path = (manifest.parent / example['image']).resolve()
            if path in seen:
                raise ValueError(f'{location}: each image file needs a single row.')
            seen.add(path)
            try:
                frame = read_planes(path, single=True)[0]
            except (OSError, ValueError, cv2.error) as exc:
                raise ValueError(f'{location}: {exc}') from exc
            if frame.shape != shape or frame.dtype.name != dtype:
                raise ValueError(f'{location}: expected {dtype} pixels with shape {shape} from the reference recording.')
            records.append(dict(example, frame=frame))
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path, help='CSV with image, field, partition, and label columns')
    parser.add_argument('--reference', required=True, type=Path, help='TEMPO Native lossless recording TIFF or its .recording.json sidecar')
    parser.add_argument('--output', required=True, type=Path, help='Path for a new candidate profile JSON file')
    args = parser.parse_args(argv)
    try:
        signature, bits, shape, dtype = read_reference(args.reference)
        records = read_examples(args.manifest, shape, dtype)
        profile = fit_profile(records, signature, sensor_bits=bits)
        with file_lock(args.output):
            if os.path.lexists(args.output):
                raise FileExistsError(f'{args.output} already exists. Choose a new output path.')
            atomic_write(args.output, profile)
    except (csv.Error, OSError, ValueError, cv2.error) as exc:
        parser.exit(1, f'Focus calibration failed: {exc}\n')

    validation = profile['validation']
    test_images = [row['image'] for row in records if row['partition'] == 'test']
    for image, result in zip(test_images, validation):
        flags = ', '.join(result['flags']) or 'none'
        reason = f" {result['reason']}" if result['reason'] else ''
        print(f"{image}: expected {result['expected']}, got {result['actual']} (flags: {flags}).{reason}")
    matches = sum(row['expected'] == row['actual'] for row in validation)
    false_sharp = sum(row['expected'] == 'out_of_focus' and row['actual'] == 'sharp' for row in validation)
    print(f'Validation: {matches}/{len(validation)} matched, {false_sharp} false Sharp.')
    print(f'Wrote candidate profile to {args.output}. Review the results before installing it in TEMPO.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
