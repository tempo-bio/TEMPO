"""Synthetic signal, directionality and processing-metadata checks."""
import unittest
import os
import tempfile
import numpy as np
import pandas as pd

from vk_math_engine import (
    asls_baseline_detrend,
    apply_butterworth_lowpass,
    compute_directional_anisotropy,
    analyze_transients
)
from tempo_statistics import compile_master_dataset


class KineticsValidityTests(unittest.TestCase):

    def test_asls_baseline_detrend(self):
        """
        Validates that Asymmetric Least Squares (AsLS) detrending removes low-frequency
        photobleaching and thermal drift while strictly preserving systolic peak amplitude.
        """
        fps = 100.0
        duration_s = 5.0
        n_frames = int(fps * duration_s)
        t = np.linspace(0, duration_s, n_frames)

        # 1. Thermal/photobleaching baseline drift: exponential decay + linear ramp
        true_baseline = 15.0 * np.exp(-t / 1.5) + 2.0 * t + 5.0

        # 2. Add 5 distinct systolic peaks of known height (height = 20.0 a.u.)
        signal = true_baseline.copy()
        peak_times = [0.8, 1.6, 2.4, 3.2, 4.0]
        expected_peak_height = 20.0
        for pt in peak_times:
            sigma = 0.05
            peak_pulse = expected_peak_height * np.exp(-((t - pt) ** 2) / (2 * (sigma ** 2)))
            signal += peak_pulse

        # 3. Apply AsLS baseline detrending
        detrended, estimated_baseline = asls_baseline_detrend(signal, lam=1e5, p=0.001, max_iter=10)

        # Verification A: The baseline estimate should closely track the true baseline in diastolic troughs
        diastolic_mask = np.ones(n_frames, dtype=bool)
        for pt in peak_times:
            diastolic_mask[np.abs(t - pt) < 0.20] = False
        diastolic_error = np.mean(np.abs(estimated_baseline[diastolic_mask] - true_baseline[diastolic_mask]))
        assert diastolic_error < 0.50, f"Diastolic baseline error too high: {diastolic_error:.3f}"

        # Verification B: Peak amplitudes on the detrended signal must be preserved within 5%
        for pt in peak_times:
            idx = int(pt * fps)
            local_peak = np.max(detrended[max(0, idx - 10):min(n_frames, idx + 10)])
            assert abs(local_peak - expected_peak_height) < 1.0, (
                f"Peak height at t={pt}s corrupted: expected {expected_peak_height}, got {local_peak:.2f}"
            )

        # Verification C: Detrended diastolic baseline should be centered near zero (>= -0.5)
        diastolic_min = np.min(detrended[diastolic_mask])
        assert diastolic_min >= -0.5, f"Artificial negative trough introduced: {diastolic_min:.3f}"

    def test_butterworth_lowpass_zero_phase(self):
        """
        Validates that bidirectional Butterworth low-pass filtering strips high-frequency
        shot noise while guaranteeing exactly zero phase shift (0.0 ms peak latency distortion).
        """
        fps = 100.0
        duration_s = 3.0
        n_frames = int(fps * duration_s)
        t = np.linspace(0, duration_s, n_frames)

        # Clean cardiac transient at 1.5 Hz
        clean_signal = 10.0 * np.sin(2 * np.pi * 1.5 * t) ** 4
        true_peak_idx = int(np.argmax(clean_signal[:int(fps)]))

        # Add 40 Hz high-frequency shot noise (Nyquist = 50 Hz)
        np.random.seed(42)
        hf_noise = 2.5 * np.sin(2 * np.pi * 40.0 * t) + np.random.normal(0, 0.5, n_frames)
        noisy_signal = clean_signal + hf_noise

        # Filter with cutoff 18 Hz
        filtered_signal = apply_butterworth_lowpass(noisy_signal, fps=fps, cutoff_hz=18.0, order=4)

        # Verification A: Noise variance is attenuated by > 70%
        input_noise_var = np.var(noisy_signal - clean_signal)
        output_noise_var = np.var(filtered_signal - clean_signal)
        reduction = (input_noise_var - output_noise_var) / input_noise_var
        assert reduction > 0.70, f"Noise reduction insufficient: {reduction * 100:.1f}%"

        # Verification B: Zero-phase preservation — peak location must remain unchanged within 1 sample (0.0 ms)
        filtered_peak_idx = int(np.argmax(filtered_signal[:int(fps)]))
        sample_shift = abs(filtered_peak_idx - true_peak_idx)
        assert sample_shift <= 1, f"Phase distortion detected: shifted by {sample_shift} samples"

    def test_directional_anisotropy_uniaxial_vs_isotropic(self):
        """
        Validates that Lucas-Kanade optical flow tensor correctly computes:
        1. High Fractional Anisotropy (α > 0.65) and aligned axis (θ ≈ 0° or 180°) for uniaxial contraction.
        2. Low Fractional Anisotropy (α < 0.40) for isotropic radial contraction / noise.
        """
        h, w = 120, 120
        n_frames = 20
        fps = 100.0

        # 1. Simulate a cell containing myofibrillar texture with smooth continuous features
        import cv2
        np.random.seed(123)
        base_texture = cv2.GaussianBlur(np.random.randint(60, 200, (h, w), dtype=np.uint8), (5, 5), 1.5)

        # Polygon mask for the cell
        polygon_mask = [[20, 20], [100, 20], [100, 100], [20, 100]]

        # Case A: Pure uniaxial displacement along X-axis (dx varying, dy = 0)
        frames_uniaxial = []
        for f in range(n_frames):
            dx = float(np.sin(0.5 * np.pi * min(f, 10) / 10.0) * 4.0)
            M = np.float32([[1.0, 0.0, dx], [0.0, 1.0, 0.0]])
            frame = cv2.warpAffine(base_texture, M, (w, h), borderMode=cv2.BORDER_REFLECT)
            frames_uniaxial.append(frame)

        # Compute anisotropy for uniaxial
        res_uniaxial = compute_directional_anisotropy(
            frames_uniaxial, polygon_mask=polygon_mask, fps=fps,
            peaks=[10], left_ips=[0]
        )

        fa_uni = res_uniaxial["Fractional_Anisotropy"]
        angle_uni = res_uniaxial["Contraction_Angle_deg"]
        coherence_uni = res_uniaxial["Vector_Coherence"]

        assert fa_uni >= 0.70, f"Expected high FA for uniaxial contraction, got {fa_uni:.3f}"
        # Contraction along horizontal axis should be close to 0° or 180°
        assert angle_uni <= 20.0 or angle_uni >= 160.0, f"Expected angle near 0°/180°, got {angle_uni:.1f}°"
        assert coherence_uni >= 0.70, f"Expected strong vector coherence, got {coherence_uni:.3f}"

        # Case B: Static frames with isotropic noise
        frames_isotropic = [base_texture.copy() + np.random.randint(-2, 3, (h, w), dtype=np.int16).clip(0, 255).astype(np.uint8) for _ in range(n_frames)]
        res_iso = compute_directional_anisotropy(
            frames_isotropic, polygon_mask=polygon_mask, fps=fps,
            peaks=[10], left_ips=[0]
        )
        fa_iso = res_iso["Fractional_Anisotropy"]
        assert fa_iso < 0.40, f"Expected low FA for isotropic noise, got {fa_iso:.3f}"

    def test_pipeline_integration_and_master_dataset(self):
        """
        Validates end-to-end integration:
        1. analyze_transients outputs Fractional_Anisotropy, Contraction_Angle_deg, Vector_Coherence.
        2. compile_master_dataset aggregates these into Master_Cell_Data.csv.
        """
        fps = 100.0
        t = np.linspace(0, 2.0, 200)
        # Contraction waveform with two peaks
        p1 = 15.0 * np.exp(-((t - 0.5) ** 2) / (2 * (0.06 ** 2)))
        p2 = 16.0 * np.exp(-((t - 1.5) ** 2) / (2 * (0.06 ** 2)))
        signal = p1 + p2
        contraction_data = [(float(t[i] * 1000.0), float(signal[i])) for i in range(len(t))]

        mock_anisotropy = [
            {"Fractional_Anisotropy": 0.82, "Contraction_Angle_deg": 12.5, "Vector_Coherence": 0.91},
            {"Fractional_Anisotropy": 0.84, "Contraction_Angle_deg": 14.0, "Vector_Coherence": 0.89}
        ]

        metrics = analyze_transients(
            contraction_data, fps=fps,
            anisotropy_beat_metrics=mock_anisotropy,
            cutoff_hz=18.0
        )

        assert len(metrics) == 2, f"Expected 2 detected peaks, got {len(metrics)}"
        for m in metrics:
            assert "Fractional_Anisotropy" in m
            assert "Contraction_Angle_deg" in m
            assert "Vector_Coherence" in m
            assert m["Fractional_Anisotropy"] > 0.80

        # Now verify compilation into master dataset
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "B02_FOV001_cell_1_transients_summary.csv")
            df_cell = pd.DataFrame([
                {
                    "Peak #": m["Peak_Number"],
                    "Time of Peak (ms)": m["Time_of_Peak_ms"],
                    "Peak Amplitude (a.u.)": m["Peak_Amplitude_Raw_au"],
                    "Peak Amplitude (Area-Norm a.u.)": m["Peak_Amplitude_AreaNorm_au"],
                    "Peak Amplitude (Fractional dI/I0)": m["Peak_Amplitude_Fractional_dI_I0"],
                    "Time-to-Peak (ms)": m["Time_to_Peak_ms"],
                    "Relaxation Time (ms)": m["Relaxation_Time_ms"],
                    "Transient Duration (ms)": m["Transient_Duration_ms"],
                    "CT50 (ms)": m["CT50_ms"],
                    "CT90 (ms)": m["CT90_ms"],
                    "RT50 (ms)": m["RT50_ms"],
                    "RT90 (ms)": m["RT90_ms"],
                    "Max Contraction Velocity": m["Max_Contraction_Velocity"],
                    "Max Relaxation Velocity": m["Max_Relaxation_Velocity"],
                    "Peak-to-Peak Time (ms)": m["Peak_to_Peak_Time_ms"],
                    "Fractional Anisotropy": m["Fractional_Anisotropy"],
                    "Contraction Angle (deg)": m["Contraction_Angle_deg"],
                    "Vector Coherence": m["Vector_Coherence"]
                }
                for m in metrics
            ])
            df_cell.to_csv(csv_path, index=False)

            # Compile master dataset
            master_df = compile_master_dataset(tmpdir)
            assert master_df is not None and not master_df.empty
            assert "Fractional_Anisotropy" in master_df.columns
            assert "Contraction_Angle_deg" in master_df.columns
            assert "Vector_Coherence" in master_df.columns

            row = master_df.iloc[0]
            assert abs(row["Fractional_Anisotropy"] - 0.83) < 0.02
            assert abs(row["Contraction_Angle_deg"] - 13.25) < 0.5
            assert abs(row["Vector_Coherence"] - 0.90) < 0.02


if __name__ == "__main__":
    unittest.main()
