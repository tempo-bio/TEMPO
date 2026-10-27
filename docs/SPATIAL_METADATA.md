# Optional spatial registration, version 1

Image-local centroids do not locate an image inside a well. A full-well map requires a calibrated affine transform into a shared physical coordinate system. Supply a calibrated transform for each field to place it on the full-well map.

For input `recording.avi`, use its sibling `recording.avi.spatial.json`:

```json
{
  "version": 1,
  "recording_id": "recording",
  "well_id": "A01",
  "field_id": "FOV001",
  "units": "um",
  "coordinate_system": "experiment-A01-calibrated",
  "image_size_px": [640, 480],
  "pixel_to_well": [[0.5, 0.0, -160.0], [0.0, -0.5, 120.0]],
  "well_geometry": {"center_x": 0.0, "center_y": 0.0, "radius": 3200.0}
}
```

The numbers above are illustrative, not calibration defaults. The input recording identity must match the video filename stem. Image dimensions must match the decoded video. The affine matrix converts `[image_x, image_y, 1]` into well X/Y in micrometres. Its linear part must be nonsingular; all numbers must be finite. Image size must contain two positive integers. Well geometry is optional and currently describes a circular well in the same coordinate system. Fields must share the coordinate-system name and compatible geometry before they can be combined.

Processing stores the supplied registration in provenance, copies it as `spatial.json` into the newly allocated exact run directory, retains `source_recording_id`, and updates `recording_id` to that run's output identity. Cell well/field identities must agree; conflicting metadata fails explicitly before extraction. The copied registration is included in artifact verification for statistics-only retry. Changed source registration changes processing provenance.

Master CSVs use processing/schema version 4, identify cells and groups with `Object_Type`, and store an optional `Spatial_Metadata_File` relative path. Loading validates that the document identifies the row's recording, well and field and that its path remains inside the results directory. Aggregation preserves an unambiguous association.

Full Well uses one spatial map, one pair of X/Y axes in micrometres, and one measurement color scale. Every represented recording/field must have valid, compatible registration. Click a field boundary on that map or choose a field in View; View → Full Well returns to the combined map. Boundaries come from the saved affine transform. The metadata-defined origin and coordinates are retained.

Missing, partial, malformed or incompatible registration makes the full-well map unavailable, with an explanation and affected fields. The all-cell table, individual field maps and well statistics remain available. There is no grid of miniature field maps. Even with complete registration, cells without valid centroids remain selectable and contribute to well statistics; their unavailable placement is reported.

Single-field axes are X (px) and Y (px), with Y increasing upward and zero at the lower end. This is a display convention: saved image coordinates and annotation coordinates are unchanged. Display names such as Cell 001 retain their stored cell_001 identity and leading zeros; repeated identities retain recording and field context.
