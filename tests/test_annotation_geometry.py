"""Nonfinite, malformed and ambiguous annotation geometry is rejected."""
import copy
import unittest

from tempo_annotations import normalize_record, validate_annotations


class AnnotationGeometryTests(unittest.TestCase):

    def setUp(self):
        self.record = {'coordinate_space': 'normalized', 'width': 100, 'height': 100,
                       'boxes': [{'class_id': 0, 'polygon': [[0, 0], [1, 0], [0, 1]]}]}

    def test_nonfinite_geometry_cannot_be_clamped_into_training_data(self):
        for space in ('normalized', 'pixels'):
            for value in (float('nan'), float('inf'), -float('inf')):
                record = copy.deepcopy(self.record)
                record['coordinate_space'] = space
                record['boxes'][0]['polygon'][1][0] = value
                with self.subTest(space=space, value=value), self.assertRaises(ValueError):
                    normalize_record(record, 100, 100)

    def test_invalid_dimensions_cannot_be_truncated_or_treated_as_true(self):
        for width in (True, 0, -1, 1.5, float('nan')):
            with self.subTest(width=width), self.assertRaises(ValueError):
                normalize_record(self.record, width, 100)

    def test_boolean_annotation_numbers_are_rejected(self):
        for key in ('width', 'class_id', 'coordinate'):
            record = copy.deepcopy(self.record)
            if key == 'width': record['width'] = True
            elif key == 'class_id': record['boxes'][0]['class_id'] = False
            else: record['boxes'][0]['polygon'][1][0] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_annotations({'frame.png': record})

    def test_malformed_annotation_structures_raise_validation_errors(self):
        for records in ({1: self.record}, {'frame.png': None},
                        {'frame.png': dict(self.record, boxes=None)},
                        {'frame.png': dict(self.record, boxes=[None])},
                        {'frame.png': dict(self.record, boxes=[{'class_id': 0, 'polygon': [None]*3}])}):
            with self.subTest(records=records), self.assertRaises(ValueError):
                validate_annotations(records)

    def test_valid_legacy_clipping_and_original_draft_are_preserved(self):
        record = {'boxes': [{'class_id': -1, 'box': [-10, 20, 60, 50]}]}
        original = copy.deepcopy(record)
        result = normalize_record(record, 100, 100)
        self.assertEqual(record, original)
        self.assertEqual(result['boxes'][0]['polygon'][0], [0, .2])
        self.assertEqual(validate_annotations({'frame.png': result}), (1, 1, 1))


if __name__ == "__main__":
    unittest.main()
