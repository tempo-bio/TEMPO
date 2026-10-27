"""Validated analysis data and identity-preserving cell summaries."""
from pathlib import Path
import numpy as np
import pandas as pd
from tempo_angles import axial_statistics

IDENTITY = ['Recording_ID', 'Well_ID', 'FOV_ID', 'Cell_ID']
TEXT_COLUMNS = set(IDENTITY + ['Object_Type', 'Trace_File', 'Spatial_Metadata_File'])
NUMERIC_COLUMNS = {
    'Centroid_X', 'Centroid_Y', 'Schema_Version', 'Total_Beats_Analyzed',
    'Peak_Amplitude_au', 'Peak_Amplitude_Raw_au', 'Peak_Amplitude_AreaNorm_au',
    'Peak_Amplitude_Fractional_dI_I0', 'Time_to_Peak_ms', 'Relaxation_Time_ms',
    'Transient_Duration_ms', 'Peak_to_Peak_Time_ms', 'CT50_ms', 'CT90_ms', 'RT50_ms', 'RT90_ms',
    'Max_Contraction_Velocity', 'Max_Relaxation_Velocity', 'Fractional_Anisotropy',
    'Contraction_Angle_deg', 'Contraction_Angle_Std_deg', 'Vector_Coherence',
}


def read_results(path):
    data = pd.read_csv(path, dtype={'Cell_ID': str, 'Well_ID': str, 'FOV_ID': str, 'Recording_ID': str})
    missing = {'Well_ID', 'Cell_ID'} - set(data.columns)
    if missing:
        raise ValueError('Missing columns: ' + ', '.join(sorted(missing)))
    if data['Cell_ID'].isna().any() or data['Cell_ID'].str.strip().eq('').any():
        raise ValueError('Results must contain identified cells.')
    for column in IDENTITY:
        if column not in data:
            data[column] = 'Unknown'
        data[column] = data[column].fillna('Unknown').astype(str).str.strip().replace('', 'Unknown')
    if 'Object_Type' not in data:
        data['Object_Type'] = 'cell'
    if not data['Object_Type'].isin(('cell', 'group')).all():
        raise ValueError('Object_Type must identify each measurement as cell or group.')
    quality_path=Path(path).parent/'quality.json'
    if Path(path).name=='Master_Cell_Data.csv' and quality_path.is_file():
        import json
        from tempo_quality import columns
        for name,value in columns(json.loads(quality_path.read_text())).items():
            data[name]=value
    for column in ('Centroid_X', 'Centroid_Y'):
        if column not in data:
            data[column] = np.nan
    for column in data.columns:
        if column not in NUMERIC_COLUMNS:
            continue
        numeric = pd.to_numeric(data[column], errors='coerce')
        invalid = data[column].notna() & numeric.isna()
        if invalid.any() or np.isinf(numeric).any():
            raise ValueError(f'{column} contains invalid or infinite numeric values.')
        data[column] = numeric
    from tempo_quality import apply_dataset_decisions
    return apply_dataset_decisions(data,path)


def summarize_cells(data):
    from tempo_quality import included_rows
    data = included_rows(data)
    if 'Object_Type' not in data:
        data['Object_Type'] = 'cell'
    if not data['Object_Type'].isin(('cell', 'group')).all():
        raise ValueError('Object_Type must identify each measurement as cell or group.')
    grouped = data.groupby(IDENTITY + ['Object_Type'], sort=False, dropna=False)
    means = grouped.mean(numeric_only=True)
    deviations = grouped.std(numeric_only=True)
    if 'Contraction_Angle_deg' in data:
        means['Contraction_Angle_deg'] = grouped['Contraction_Angle_deg'].agg(lambda x: axial_statistics(x)[0])
        deviations['Contraction_Angle_deg'] = grouped['Contraction_Angle_deg'].agg(lambda x: axial_statistics(x)[1])
        single = grouped.size() == 1
        deviations.loc[single, 'Contraction_Angle_deg'] = np.nan
        if 'Contraction_Angle_Std_deg' in data:
            deviations.loc[single, 'Contraction_Angle_deg'] = grouped['Contraction_Angle_Std_deg'].first()[single]
    for column in ('Trace_File', 'Spatial_Metadata_File','Quality_Flags','Focus_Status','Quality_Method',
                   'Quality_Decision_Actor','Quality_Decision_Reason','Source_Pixel_Dtype','Timing_Basis'):
        if column in data:
            # Ambiguous associations must never select a different cell/field.
            means[column] = grouped[column].agg(lambda x: x.dropna().iloc[0] if x.dropna().nunique() == 1 else '')
    return means.reset_index(), deviations.reset_index()


def read_trace(dataset_path, row, signal):
    relative = row.get('Trace_File')
    if not isinstance(relative, str) or not relative:
        raise ValueError('Recorded trace is unavailable for this result.')
    base = Path(dataset_path).resolve().parent
    path = (base / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError('Trace path must remain within the results directory.')
    frame = pd.read_csv(path)
    if 'Time (ms)' not in frame or signal not in frame:
        raise ValueError('Selected signal is unavailable in this trace.')
    values = frame[['Time (ms)', signal]].apply(pd.to_numeric, errors='raise')
    if values.empty or not np.isfinite(values.to_numpy()).all():
        raise ValueError('Trace contains missing or invalid values.')
    if (values['Time (ms)'].diff().dropna() <= 0).any():
        raise ValueError('Trace times must increase.')
    return values.iloc[:, 0].to_numpy(), values.iloc[:, 1].to_numpy()
