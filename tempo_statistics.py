from tempo_angles import axial_statistics
from tempo_processing import SCHEMA_VERSION, file_lock
from tempo_regions import object_type
import cv2
import os
import pandas as pd
import glob
import re
import numpy as np
import tempfile
from pathlib import Path

SUMMARY_NUMERIC_COLUMNS = {
    'Peak #', 'Time of Peak (ms)', 'Peak Amplitude (a.u.)',
    'Peak Amplitude (Raw a.u.)', 'Peak Amplitude (Area-Norm a.u.)',
    'Peak Amplitude (Fractional dI/I0)', 'Time-to-Peak (ms)',
    'Relaxation Time (ms)', 'Transient Duration (ms)', 'Peak-to-Peak Time (ms)',
    'CT50 (ms)', 'CT90 (ms)', 'RT50 (ms)', 'RT90 (ms)',
    'Max Contraction Velocity', 'Max Relaxation Velocity',
    'Fractional Anisotropy', 'Contraction Angle (deg)', 'Vector Coherence',
}

def compile_master_dataset(processed_run_folder, cells_data=None):
    """
    Searches a processed output folder for all transient summary CSVs,
    averages the beats per cell, and compiles a master DataFrame.
    """
    summaries = {Path(file).name.removesuffix('_transients_summary.csv'): file
                 for file in glob.glob(os.path.join(processed_run_folder, '*_transients_summary.csv'))}
    traces = {Path(file).name.removesuffix('_raw_kinematics.csv'): file
              for file in glob.glob(os.path.join(processed_run_folder, '*_raw_kinematics.csv'))}
    cell_ids = sorted(summaries.keys() | traces.keys())
    if not cell_ids:
        return None

    all_cells_data = []
    
    # Helper mapping if manual GUI cells_data was provided
    cell_meta_map = {}
    if cells_data:
        for c in cells_data:
            # Map standard id or the prepended "masked_" id to its metadata
            cell_meta_map[c.get("id", "")] = c
            cell_meta_map[f"masked_{c.get('id', '')}"] = c

    for cell_id in cell_ids:
        file = summaries.get(cell_id)
        filename = f'{cell_id}_transients_summary.csv'
        
        # Load the cell's data
        try:
            df = pd.read_csv(file) if file else pd.DataFrame()
        except Exception as e:
            raise ValueError(f"Could not read summary {filename}: {e}") from e
            
        # Keep a trace-only cell inspectable without inventing beat measurements.
        if df.empty and cell_id not in traces:
            continue
        if not df.empty and not (SUMMARY_NUMERIC_COLUMNS - {'Peak #', 'Time of Peak (ms)'}).intersection(df.columns):
            raise ValueError(f'No recognized beat measurements in {filename}')
        for column in SUMMARY_NUMERIC_COLUMNS.intersection(df.columns):
            numbers = pd.to_numeric(df[column], errors='coerce')
            if (df[column].notna() & numbers.isna()).any() or np.isinf(numbers).any():
                raise ValueError(f'Invalid numeric values in {filename}: {column}')
            df[column] = numbers
        # Ignore the 0.0 value in the first beat's Peak-to-Peak time
        if "Peak-to-Peak Time (ms)" in df.columns:
            df["Peak-to-Peak Time (ms)"] = df["Peak-to-Peak Time (ms)"].replace(0.0, np.nan)
            
        # A beating cell has multiple peaks. We want the average physiological 
        # behavior of this specific cell across its entire recording.
        cell_averages = df.mean(numeric_only=True)
        
        # Attempt to grab manual metadata, or auto-extract from filename/folder regex
        meta = cell_meta_map.get(cell_id, {})
        well_id = meta.get("well_id", "")
        fov_id = meta.get("fov_id", "")

        if not well_id or not fov_id:
            search_str = f"{os.path.basename(processed_run_folder)}_{filename}"
            regex_match = re.search(r"(?P<well>[A-H]\d{2})_(?P<fov>FOV\d+)", search_str, re.IGNORECASE)
            if regex_match:
                if not well_id: well_id = regex_match.group("well").upper()
                if not fov_id: fov_id = regex_match.group("fov").upper()

        if not well_id:
            well_id = "Unknown"
        if not fov_id:
            fov_id = "Unknown"
            
        cx, cy = meta.get('centroid_x'), meta.get('centroid_y')
        if (cx is None or cy is None) and meta.get('polygon'):
            moments = cv2.moments(np.asarray(meta['polygon'], dtype=np.float32))
            if moments['m00']:
                cx, cy = moments['m10'] / moments['m00'], moments['m01'] / moments['m00']
        def coordinate(value):
            try:
                number = float(value)
                return number if np.isfinite(number) else np.nan
            except (TypeError, ValueError):
                return np.nan
        cx, cy = coordinate(cx), coordinate(cy)
        angle_mean, angle_std = axial_statistics(df.get('Contraction Angle (deg)', []))

        # Build a dictionary for this cell
        cell_summary = {
            "Schema_Version": SCHEMA_VERSION,
            "Recording_ID": os.path.basename(processed_run_folder),
            "Trace_File": f"{cell_id}_raw_kinematics.csv",
            "Spatial_Metadata_File": meta.get("spatial_metadata_file", ""),
            "Contraction_Angle_Std_deg": angle_std,
            "Well_ID": well_id,
            "FOV_ID": fov_id,
            "Cell_ID": cell_id,
            "Object_Type": object_type(meta),
            "Centroid_X": cx,
            "Centroid_Y": cy,
            "Total_Beats_Analyzed": len(df),
            # --- FORMATTED EXACTLY FOR THE ANALYZER SUITE ---
            "Peak_Amplitude_au": cell_averages.get("Peak Amplitude (a.u.)"),
            "Peak_Amplitude_Raw_au": cell_averages.get("Peak Amplitude (Raw a.u.)", cell_averages.get("Peak Amplitude (a.u.)")),
            "Peak_Amplitude_AreaNorm_au": cell_averages.get("Peak Amplitude (Area-Norm a.u.)", np.nan),
            "Peak_Amplitude_Fractional_dI_I0": cell_averages.get("Peak Amplitude (Fractional dI/I0)", np.nan),
            "Time_to_Peak_ms": cell_averages.get("Time-to-Peak (ms)"),
            "Relaxation_Time_ms": cell_averages.get("Relaxation Time (ms)"),
            "Transient_Duration_ms": cell_averages.get("Transient Duration (ms)"),
            "Peak_to_Peak_Time_ms": cell_averages.get("Peak-to-Peak Time (ms)"),
            "CT50_ms": cell_averages.get("CT50 (ms)"),
            "CT90_ms": cell_averages.get("CT90 (ms)"),
            "RT50_ms": cell_averages.get("RT50 (ms)"),
            "RT90_ms": cell_averages.get("RT90 (ms)"),
            "Max_Contraction_Velocity": cell_averages.get("Max Contraction Velocity"),
            "Max_Relaxation_Velocity": cell_averages.get("Max Relaxation Velocity"),
            "Fractional_Anisotropy": cell_averages.get("Fractional Anisotropy", np.nan),
            "Contraction_Angle_deg": angle_mean,
            "Vector_Coherence": cell_averages.get("Vector Coherence", np.nan),
        }
        
        from tempo_capture import capture_columns
        cell_summary.update(capture_columns(meta.get('capture_metadata')))
        source_format=meta.get('source_format',{})
        cell_summary.update(Source_Pixel_Dtype=source_format.get('dtype','unknown'),
            Source_Sensor_Bits=source_format.get('sensor_bits'),Timing_Basis=source_format.get('timing_basis','unknown'))
        quality_path = os.path.join(processed_run_folder,'quality.json')
        if os.path.isfile(quality_path):
            import json
            from tempo_quality import columns
            with open(quality_path) as stream:
                cell_summary.update(columns(json.load(stream)))
        all_cells_data.append(cell_summary)

    # Convert the compiled list into a master Pandas DataFrame
    master_df = pd.DataFrame(all_cells_data)
    return master_df

def generate_run_statistics(master_df, output_folder, replace_existing=False):
    """
    Takes the master dataset and calculates the global mean and standard deviation
    across all healthy cells in the run.
    """
    if master_df is None or master_df.empty:
        return False
        
    # Save the Master CSV (Every cell's individual average)
    master_csv_path = os.path.join(output_folder, "Master_Cell_Data.csv")
    with file_lock(master_csv_path):
        if os.path.exists(master_csv_path) and not replace_existing:
            raise FileExistsError('Existing research statistics will not be overwritten. Choose a new output directory.')
        fd, temporary = tempfile.mkstemp(prefix='Master_Cell_Data.', suffix='.tmp', dir=output_folder)
        try:
            with os.fdopen(fd, 'w') as stream:
                master_df.to_csv(stream, index=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, master_csv_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print(f"[STATS] Compiled Master Dataset saved to: {os.path.basename(master_csv_path)}")
    
    return True

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Compile a supplied run without replacing existing statistics.')
    parser.add_argument('run_folder')
    args = parser.parse_args()
    generate_run_statistics(compile_master_dataset(args.run_folder), args.run_folder)
