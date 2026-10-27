from tempo_angles import axial_statistics
from tempo_recording import MaskedFrames, FrameSlice, display_frame
import cv2
import numpy as np
import os
import csv
from itertools import islice
from scipy.signal import find_peaks, peak_widths, savgol_filter, butter, filtfilt
from scipy import sparse
from scipy.sparse.linalg import spsolve

def autodetect_reference_frame(cap_or_frames, start_frame=1, stop_frame=300, low_value_n=20, unity_selection_n=10, cancel=None):
    """Finds the most relaxed frame to use as the baseline from VideoCapture or in-memory frames."""
    speed_y = []
    
    if isinstance(cap_or_frames, (list, tuple, np.ndarray, MaskedFrames)):
        frames = cap_or_frames
        if len(frames) == 0: return 0
        prev_frame = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY).astype(np.float32) if len(frames[0].shape) == 3 else frames[0].astype(np.float32)
        stop_idx = min(len(frames), stop_frame)
        for frame in islice(frames,1,stop_idx):
            if cancel:
                cancel.check()
            curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) if len(frame.shape) == 3 else frame.astype(np.float32)
            diff = cv2.absdiff(curr_gray, prev_frame)
            speed_y.append(np.mean(diff))
            prev_frame = curr_gray
    else:
        cap = cap_or_frames
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0) # Reset to beginning
        ret, prev_frame = cap.read()
        if not ret: return 1
        prev_frame = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        
        frame_idx = 1
        while frame_idx <= stop_frame:
            if cancel:
                cancel.check()
            ret, current_frame = cap.read()
            if not ret: break
            current_frame_gray = cv2.cvtColor(current_frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            diff = cv2.absdiff(current_frame_gray, prev_frame)
            speed_y.append(np.mean(diff))
            prev_frame = current_frame_gray.copy()
            frame_idx += 1

    speed_y = np.array(speed_y[start_frame:stop_frame])
    if len(speed_y) < 2: return 1

    speed_y_shift, speed_y_base = speed_y[1:], speed_y[:-1]
    radian_points = np.sqrt((speed_y_base**2) + (speed_y_shift**2))
    indices_val = np.argsort(radian_points)
    
    unity_selection, valid_indices = [], []
    for i in range(min(low_value_n - 1, len(indices_val))):
        idx = indices_val[i]
        
        # --- SYNTHETIC DATA FIX ---
        # If speed is exactly 0.0, we found a mathematically perfect resting frame.
        if speed_y_shift[idx] == 0:
            return idx + 1 + start_frame
            
        elif speed_y_shift[idx] != 0:
            unity_selection.append(abs((speed_y_base[idx] / speed_y_shift[idx]) - 1))
            valid_indices.append(idx)
            
    if not unity_selection: return 1
    indices_uni = np.argsort(unity_selection)
    
    low, low_index = float('inf'), 0
    for i in range(min(unity_selection_n - 1, len(indices_uni))):
        idx_trans = indices_uni[i]
        idx = valid_indices[idx_trans]
        low_value = (speed_y_base[idx] * speed_y_shift[idx]) * unity_selection[idx_trans]
        if low_value < low:
            low, low_index = low_value, idx

    return low_index + 1 + start_frame


def asls_baseline_detrend(y, lam=100000.0, p=0.001, max_iter=10):
    """
    Asymmetric Least Squares (AsLS) Baseline Detrending (Eilers & Boelens, 2005).
    Anchors strictly to resting diastolic valleys (p=0.001) while regularizing curvature (lam=1e5),
    eliminating slow thermal lamp heating, LED decay, and cellular focal drift without attenuating systolic peaks.
    """
    y = np.asarray(y, dtype=np.float64)
    L = len(y)
    if L < 4:
        return y, np.zeros_like(y)
        
    diagonals = [np.ones(L), -2.0 * np.ones(L), np.ones(L)]
    D = sparse.spdiags(diagonals, [0, 1, 2], L - 2, L)
    DTD = D.T.dot(D)
    
    w = np.ones(L)
    z = y.copy()
    
    for _ in range(max_iter):
        W = sparse.spdiags(w, 0, L, L)
        A = W + lam * DTD
        b = w * y
        z_new = spsolve(A, b)
        
        # Asymmetric weighting: p for positive deviations (peaks), 1-p for valleys
        w_new = np.where(y > z_new, p, 1.0 - p)
        
        if np.max(np.abs(z_new - z)) < 1e-5:
            z = z_new
            break
        z = z_new
        w = w_new
        
    y_detrended = np.maximum(0.0, y - z)
    return y_detrended, z


def apply_butterworth_lowpass(y, fps=None, cutoff_hz=18.0, order=4):
    """
    Adaptive Zero-Phase Butterworth Low-Pass Filter (Robertson & Dowling, 2003).
    Applies bidirectional forward-backward filtering (filtfilt) to achieve effective
    8th-order roll-off (-48 dB/octave) with strictly zero phase lag (0.0 ms delay).
    Cutoff defaults to 18.0 Hz (retaining >99.5% cardiomyocyte twitch power while stripping
    high-frequency CMOS Poisson shot noise and 60/120 Hz line flicker).
    """
    y = np.asarray(y, dtype=np.float64)
    L = len(y)
    if fps is None or fps <= 0:
        fps = 100.0
        
    nyq = 0.5 * fps
    norm_cutoff = cutoff_hz / nyq
    
    if norm_cutoff >= 0.95:
        norm_cutoff = 0.95
    elif norm_cutoff <= 0.01:
        norm_cutoff = 0.01
        
    padlen = 3 * (2 * order + 1)
    if L <= padlen:
        return y.copy()
        
    try:
        b, a = butter(order, norm_cutoff, btype='low', analog=False)
        y_filtered = filtfilt(b, a, y)
        return np.maximum(0.0, y_filtered)
    except Exception as e:
        print(f"[VK ENGINE] Warning: Butterworth filter fallback ({e})")
        return y.copy()


def compute_directional_anisotropy(frame_sequence, polygon_mask=None, fps=None, peaks=None, left_ips=None, cancel=None):
    """
    Directional Anisotropy via Sparse Lucas-Kanade Vector Flow & Covariance Eigendecomposition.
    Tracks internal myofibril features within the cell polygon mask across contraction upstrokes.
    Constructs the spatial covariance tensor:
        C = (1/N) * Σ [ Δx_i²      Δx_i*Δy_i ]
                      [ Δx_i*Δy_i  Δy_i²     ]
    Extracts:
        - Fractional Anisotropy (α ∈ [0.0, 1.0]): Uniaxial alignment along sarcomeres (α ≥ 0.75 mature, α < 0.35 isotropic).
        - Principal Contraction Angle (θ ∈ [0°, 180°]): Orientation of primary contraction axis.
        - Intra-Cellular Vector Coherence (∈ [0.0, 1.0]): Mechanical synchrony across the cellular body.
    """
    if frame_sequence is None or len(frame_sequence) < 2:
        return {"Fractional_Anisotropy": 0.0, "Contraction_Angle_deg": float("nan"), "Vector_Coherence": 0.0, "beat_metrics": []}
        
    num_frames = len(frame_sequence)
    ref_img = frame_sequence[0]
    ref_gray = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY) if len(ref_img.shape) == 3 else ref_img.copy()
    if ref_gray.dtype != np.uint8:
        ref_gray = display_frame(ref_gray)
            
    # Erode polygon mask by 2px to avoid detecting corners on artificial canvas crop edges
    mask_eroded = None
    if polygon_mask is not None:
        if isinstance(polygon_mask, np.ndarray) and polygon_mask.shape == ref_gray.shape:
            p_mask = (polygon_mask > 0).astype(np.uint8) * 255
        else:
            p_mask = np.zeros(ref_gray.shape, dtype=np.uint8)
            try:
                pts = np.array(polygon_mask, dtype=np.int32).reshape((-1, 1, 2))
                cv2.fillPoly(p_mask, [pts], 255)
            except Exception:
                p_mask.fill(255)
                
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask_eroded = cv2.erode(p_mask, kernel, iterations=2)
        if np.count_nonzero(mask_eroded) < 16:
            mask_eroded = p_mask
            
    # Detect optimal internal corners strictly inside the cell mask
    feature_pts = cv2.goodFeaturesToTrack(
        ref_gray,
        maxCorners=35,
        qualityLevel=0.01,
        minDistance=4,
        mask=mask_eroded
    )
    
    # Fallback: regular interior sampling if texture contrast is uniform
    if feature_pts is None or len(feature_pts) < 4:
        target_mask = mask_eroded if mask_eroded is not None else np.ones_like(ref_gray, dtype=np.uint8)
        y_indices, x_indices = np.where(target_mask > 0)
        if len(x_indices) >= 4:
            step = max(1, len(x_indices) // 30)
            sampled_x = x_indices[::step][:30]
            sampled_y = y_indices[::step][:30]
            feature_pts = np.float32([[x, y] for x, y in zip(sampled_x, sampled_y)]).reshape(-1, 1, 2)
            
    if feature_pts is None or len(feature_pts) < 4:
        return {"Fractional_Anisotropy": 0.0, "Contraction_Angle_deg": float("nan"), "Vector_Coherence": 0.0, "beat_metrics": []}
        
    beat_metrics = []
    
    # If peaks are supplied, analyze motion across each systolic upstroke
    if peaks is not None and len(peaks) > 0:
        for k in range(len(peaks)):
            if cancel:
                cancel.check()
            peak_idx = int(peaks[k])
            start_idx = int(left_ips[k]) if (left_ips is not None and k < len(left_ips)) else max(0, peak_idx - 10)
            start_idx = max(0, min(start_idx, num_frames - 1))
            peak_idx = max(0, min(peak_idx, num_frames - 1))
            
            if peak_idx <= start_idx:
                beat_metrics.append({"Fractional_Anisotropy": 0.0, "Contraction_Angle_deg": float("nan"), "Vector_Coherence": 0.0})
                continue
                
            f_start = frame_sequence[start_idx]
            f_peak = frame_sequence[peak_idx]
            g_start = cv2.cvtColor(f_start, cv2.COLOR_BGR2GRAY) if len(f_start.shape) == 3 else f_start
            g_peak = cv2.cvtColor(f_peak, cv2.COLOR_BGR2GRAY) if len(f_peak.shape) == 3 else f_peak
            if g_start.dtype != np.uint8:
                g_start = display_frame(g_start)
            if g_peak.dtype != np.uint8:
                g_peak = display_frame(g_peak)
                
            next_pts, status, _ = cv2.calcOpticalFlowPyrLK(
                g_start, g_peak, feature_pts, None,
                winSize=(15, 15), maxLevel=2,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03)
            )
            
            valid_idx = np.where(status.ravel() == 1)[0]
            if len(valid_idx) < 3:
                beat_metrics.append({"Fractional_Anisotropy": 0.0, "Contraction_Angle_deg": float("nan"), "Vector_Coherence": 0.0})
                continue
                
            disp = next_pts[valid_idx].reshape(-1, 2) - feature_pts[valid_idx].reshape(-1, 2)
            dx = disp[:, 0]
            dy = disp[:, 1]
            
            # Covariance tensor components
            N_pts = len(disp)
            C_xx = float(np.sum(dx * dx) / N_pts)
            C_yy = float(np.sum(dy * dy) / N_pts)
            C_xy = float(np.sum(dx * dy) / N_pts)
            
            tr = C_xx + C_yy
            det = C_xx * C_yy - C_xy * C_xy
            disc = max(0.0, tr * tr - 4.0 * det)
            sqrt_disc = np.sqrt(disc)
            lambda1 = 0.5 * (tr + sqrt_disc)
            lambda2 = max(0.0, 0.5 * (tr - sqrt_disc))
            
            denom = lambda1 + lambda2
            fa = float((lambda1 - lambda2) / (denom + 1e-9)) if denom > 1e-9 else 0.0
            fa = max(0.0, min(1.0, fa))
            
            angle_rad = 0.5 * np.arctan2(2.0 * C_xy, C_xx - C_yy)
            angle_deg = float(np.degrees(angle_rad)) % 180.0 if sqrt_disc > 1e-9 else float("nan")
            
            # Vector Coherence: ||Σ v_i|| / (Σ ||v_i||)
            sum_vec = np.sum(disp, axis=0)
            mag_sum = np.linalg.norm(sum_vec)
            sum_mag = np.sum(np.linalg.norm(disp, axis=1))
            coherence = float(mag_sum / (sum_mag + 1e-9))
            coherence = max(0.0, min(1.0, coherence))
            
            beat_metrics.append({
                "Fractional_Anisotropy": fa,
                "Contraction_Angle_deg": angle_deg,
                "Vector_Coherence": coherence
            })
    else:
        mid_idx = min(num_frames - 1, 30)
        f_start = frame_sequence[0]
        f_mid = frame_sequence[mid_idx]
        g_start = cv2.cvtColor(f_start, cv2.COLOR_BGR2GRAY) if len(f_start.shape) == 3 else f_start
        g_mid = cv2.cvtColor(f_mid, cv2.COLOR_BGR2GRAY) if len(f_mid.shape) == 3 else f_mid
        if g_start.dtype != np.uint8:
            g_start = display_frame(g_start)
        if g_mid.dtype != np.uint8:
            g_mid = display_frame(g_mid)
        next_pts, status, _ = cv2.calcOpticalFlowPyrLK(g_start, g_mid, feature_pts, None)
        valid_idx = np.where(status.ravel() == 1)[0]
        if len(valid_idx) >= 3:
            disp = next_pts[valid_idx].reshape(-1, 2) - feature_pts[valid_idx].reshape(-1, 2)
            dx, dy = disp[:, 0], disp[:, 1]
            N_pts = len(disp)
            C_xx = float(np.sum(dx * dx) / N_pts)
            C_yy = float(np.sum(dy * dy) / N_pts)
            C_xy = float(np.sum(dx * dy) / N_pts)
            tr = C_xx + C_yy
            det = C_xx * C_yy - C_xy * C_xy
            disc = max(0.0, tr * tr - 4.0 * det)
            lambda1 = 0.5 * (tr + np.sqrt(disc))
            lambda2 = max(0.0, 0.5 * (tr - np.sqrt(disc)))
            fa = float((lambda1 - lambda2) / (lambda1 + lambda2 + 1e-9))
            angle_deg = float(np.degrees(0.5 * np.arctan2(2.0 * C_xy, C_xx - C_yy))) % 180.0 if disc > 1e-18 else float("nan")
            coherence = float(np.linalg.norm(np.sum(disp, axis=0)) / (np.sum(np.linalg.norm(disp, axis=1)) + 1e-9))
            beat_metrics.append({"Fractional_Anisotropy": max(0.0, min(1.0, fa)), "Contraction_Angle_deg": angle_deg, "Vector_Coherence": max(0.0, min(1.0, coherence))})
            
    if beat_metrics:
        avg_fa = float(np.mean([b["Fractional_Anisotropy"] for b in beat_metrics]))
        avg_angle = axial_statistics([b["Contraction_Angle_deg"] for b in beat_metrics])[0]
        avg_coherence = float(np.mean([b["Vector_Coherence"] for b in beat_metrics]))
    else:
        avg_fa, avg_angle, avg_coherence = 0.0, float("nan"), 0.0
        
    return {
        "Fractional_Anisotropy": avg_fa,
        "Contraction_Angle_deg": avg_angle,
        "Vector_Coherence": avg_coherence,
        "beat_metrics": beat_metrics
    }


def analyze_transients(contraction_data, fps=100.0, area_norm_data=None, fractional_data=None, anisotropy_beat_metrics=None, cutoff_hz=18.0, cancel=None,
                       enable_lowpass=True, savgol_window_pct=7, peak_prominence_pct=30):
    """Calculates physiological metrics (Time-to-Peak, Relaxation, CT/RT, Velocities, Dual Amplitudes, and Anisotropy) using SciPy."""
    if not contraction_data: return []

    # Extract raw Y values
    y_raw = np.array([pt[1] for pt in contraction_data], dtype=np.float64)
    times = np.array([pt[0] for pt in contraction_data], dtype=np.float64)
    time_step = 1000.0 / fps

    # Apply adaptive zero-phase Butterworth low-pass filter (Robertson & Dowling, 2003)
    # Eliminates high-frequency CMOS sensor shot noise with strictly zero phase lag (Phi_net = 0 rad)
    y = apply_butterworth_lowpass(y_raw, fps=fps, cutoff_hz=cutoff_hz) if enable_lowpass else y_raw

    # Match MUSCLEMOTION defaults: 20-frame window, 30% peak threshold
    distance = max(1, int(fps * 0.20))
    prominence_thresh = (np.max(y) - np.min(y)) * peak_prominence_pct / 100.0

    peaks, _ = find_peaks(y, distance=distance, prominence=prominence_thresh)

    if len(peaks) == 0:
        return []

    # Calculate widths at 50% and 90% (rel_height is % down from the peak)
    widths_90, _, left_ips_90, right_ips_90 = peak_widths(y, peaks, rel_height=0.90)
    widths_50, _, left_ips_50, right_ips_50 = peak_widths(y, peaks, rel_height=0.50)
    
    # Calculate the continuous analytical derivative on the Butterworth-denoised signal
    # using Savitzky-Golay filtering (~7% of FPS window length)
    window_length = int(fps * savgol_window_pct / 100.0)
    if window_length % 2 == 0:
        window_length += 1
    if window_length < 5:
        window_length = 5  # A cubic fit needs at least five samples in an odd window.
        
    if len(y) > window_length:
        dy = savgol_filter(y, window_length=window_length, polyorder=3, deriv=1, delta=time_step)
    else:
        dy = np.diff(y) / time_step 
        dy = np.append(dy, 0) # Pad to match the length of y

    metrics = []
    for i in range(len(peaks)):
        if cancel:
            cancel.check()
        peak_idx = peaks[i]
        
        # 1. Temporal Metrics & Fractional Kinetics (Using sub-frame interpolation)
        time_to_peak = (peak_idx - left_ips_90[i]) * time_step
        relaxation_time = (right_ips_90[i] - peak_idx) * time_step
        transient_duration = widths_90[i] * time_step
        
        ct50 = (peak_idx - left_ips_50[i]) * time_step
        ct90 = time_to_peak
        
        rt50 = (right_ips_50[i] - peak_idx) * time_step
        rt90 = relaxation_time

        # 2. Velocity Extraction (Search within the bounds of this specific heartbeat)
        start_idx = max(0, int(left_ips_90[i]))
        end_idx = min(len(y)-1, int(right_ips_90[i]))

        # Max contraction velocity (steepest positive slope before peak)
        max_contraction_vel = np.max(dy[start_idx:peak_idx]) if start_idx < peak_idx else 0.0
            
        # Max relaxation velocity (steepest negative slope after peak)
        max_relaxation_vel = abs(np.min(dy[peak_idx:end_idx])) if peak_idx < end_idx else 0.0

        peak_to_peak = (times[peaks[i]] - times[peaks[i-1]]) if i > 0 else 0.0

        area_norm_val = area_norm_data[peak_idx] if (area_norm_data is not None and peak_idx < len(area_norm_data)) else y[peak_idx]
        fractional_val = fractional_data[peak_idx] if (fractional_data is not None and peak_idx < len(fractional_data)) else 0.0

        # Anisotropy metrics for this beat
        fa_val = 0.0
        ang_val = float('nan')
        coh_val = 0.0
        if anisotropy_beat_metrics and i < len(anisotropy_beat_metrics):
            fa_val = float(anisotropy_beat_metrics[i].get("Fractional_Anisotropy", 0.0))
            ang_val = float(anisotropy_beat_metrics[i].get("Contraction_Angle_deg", float('nan')))
            coh_val = float(anisotropy_beat_metrics[i].get("Vector_Coherence", 0.0))

        metrics.append({
            "Peak_Number": i + 1,
            "Time_of_Peak_ms": times[peak_idx],
            "Peak_Amplitude_au": y[peak_idx], # Legacy key for MUSCLEMOTION compatibility
            "Peak_Amplitude_Raw_au": y[peak_idx],
            "Peak_Amplitude_AreaNorm_au": area_norm_val,
            "Peak_Amplitude_Fractional_dI_I0": fractional_val,
            "Time_to_Peak_ms": time_to_peak,
            "Relaxation_Time_ms": relaxation_time,
            "Transient_Duration_ms": transient_duration,
            "CT50_ms": ct50,
            "CT90_ms": ct90,
            "RT50_ms": rt50,
            "RT90_ms": rt90,
            "Max_Contraction_Velocity": max_contraction_vel,
            "Max_Relaxation_Velocity": max_relaxation_vel,
            "Peak_to_Peak_Time_ms": peak_to_peak,
            "Fractional_Anisotropy": fa_val,
            "Contraction_Angle_deg": ang_val,
            "Vector_Coherence": coh_val
        })

    return metrics


def process_cell_kinematics(video_source, output_dir, cell_id, fps=None, polygon_mask=None, cancel=None, kinetics_config=None,
                            times_ms=None):
    """
    Calculates contraction/speed and extracts transient metrics with 3-tier amplitude support,
    AsLS baseline detrending, zero-phase Butterworth low-pass filtering, and directional anisotropy.
    
    Supports both file paths (str) and in-memory frame sequences (list/np.ndarray).
    """
    cap = None
    if not isinstance(video_source, (list, tuple, np.ndarray, MaskedFrames)):
        if not os.path.exists(video_source):
            return False
        cap = cv2.VideoCapture(video_source)
    try:
        return _process_cell_kinematics(video_source, output_dir, cell_id, fps,
                                        polygon_mask, cancel, kinetics_config, cap, times_ms)
    finally:
        if cap is not None:
            cap.release()


def _process_cell_kinematics(video_source, output_dir, cell_id, fps,
                             polygon_mask, cancel, kinetics_config, cap, times_ms=None):
    is_stream = isinstance(video_source, (list, tuple, np.ndarray, MaskedFrames))
    frames = []
    
    if not is_stream:
        if fps is None:
            video_fps = cap.get(cv2.CAP_PROP_FPS)
            fps = video_fps if video_fps > 0 else None
        if fps is None or fps <= 0:
            raise ValueError('Frame rate is required for time-dependent measurements.')
            
        ref_frame_idx = autodetect_reference_frame(cap, cancel=cancel)
        cap.set(cv2.CAP_PROP_POS_FRAMES, ref_frame_idx)
        ret, ref_img = cap.read()
        if not ret: 
            return False
        reference_frame = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, prev_img = cap.read()
        if not ret:
            return False
        previous_frame = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    else:
        frames = video_source
        if len(frames) == 0: return False
        if fps is None or fps <= 0:
            raise ValueError('Frame rate is required for time-dependent measurements.')
        
        ref_frame_idx = autodetect_reference_frame(frames, cancel=cancel)
        ref_idx = min(max(0, int(ref_frame_idx)), len(frames) - 1)
        ref_img = frames[ref_idx]
        reference_frame = cv2.cvtColor(ref_img, cv2.COLOR_BGR2GRAY).astype(np.float32) if len(ref_img.shape) == 3 else ref_img.astype(np.float32)
        prev_img = frames[0]
        previous_frame = cv2.cvtColor(prev_img, cv2.COLOR_BGR2GRAY).astype(np.float32) if len(prev_img.shape) == 3 else prev_img.astype(np.float32)

    # Pre-calculate baseline resting intensity inside mask for fractional amplitude
    mask_bool = None
    poly_pixel_count = 0
    i0_baseline = float(np.mean(reference_frame))
    if polygon_mask is not None:
        mask_bool = (polygon_mask > 0)
        poly_pixel_count = int(np.count_nonzero(mask_bool))
        if poly_pixel_count > 0:
            i0_baseline = float(np.mean(reference_frame[mask_bool]))
    if i0_baseline <= 0:
        i0_baseline = 1.0

    contraction_data = []
    area_norm_data = []
    fractional_data = []
    speed_data = []
    time_ms = 0
    time_step = 1000.0 / fps
    
    # The Core Frame Loop
    if not is_stream:
        while True:
            if cancel and cancel.is_cancelled():
                cancel.check()
            ret, frame = cap.read()
            if not ret: break
            frames.append(frame)
                
            current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
            
            # Contraction
            diff_matrix = cv2.absdiff(current_frame, reference_frame)
            raw_amplitude = float(np.mean(diff_matrix))
            contraction_data.append((time_ms, raw_amplitude))
            
            # Area-normalized amplitude (strictly within polygon)
            if mask_bool is not None and poly_pixel_count > 0:
                area_norm_amp = float(np.sum(diff_matrix[mask_bool]) / poly_pixel_count)
            else:
                area_norm_amp = raw_amplitude
            area_norm_data.append(area_norm_amp)
            
            # Fractional amplitude (change relative to resting baseline)
            frac_amp = area_norm_amp / i0_baseline
            fractional_data.append(frac_amp)

            # Speed
            speed_matrix = cv2.absdiff(current_frame, previous_frame)
            speed_data.append((time_ms, float(np.mean(speed_matrix))))
            
            previous_frame = current_frame.copy()
            time_ms += time_step
    else:
        for frame in frames:
            if cancel:
                cancel.check()
            current_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32) if len(frame.shape) == 3 else frame.astype(np.float32)
            
            # Contraction
            diff_matrix = cv2.absdiff(current_frame, reference_frame)
            raw_amplitude = float(np.mean(diff_matrix))
            contraction_data.append((time_ms, raw_amplitude))
            
            # Area-normalized amplitude (strictly within polygon)
            if mask_bool is not None and poly_pixel_count > 0:
                area_norm_amp = float(np.sum(diff_matrix[mask_bool]) / poly_pixel_count)
            else:
                area_norm_amp = raw_amplitude
            area_norm_data.append(area_norm_amp)
            
            # Fractional amplitude (change relative to resting baseline)
            frac_amp = area_norm_amp / i0_baseline
            fractional_data.append(frac_amp)

            # Speed
            speed_matrix = cv2.absdiff(current_frame, previous_frame)
            speed_data.append((time_ms, float(np.mean(speed_matrix))))
            
            previous_frame = current_frame.copy()
            time_ms += time_step

    if cancel:
        cancel.check()

    # Preserve acquisition time in exported samples. Uniform-time filters below
    # operate within continuous segments only; they never bridge frame gaps.
    timing_segments = None
    if times_ms is not None:
        times = np.asarray(times_ms,dtype=float)
        if len(times)!=len(contraction_data) or not np.isfinite(times).all() or np.any(np.diff(times)<=0):
            raise ValueError('Frame timing does not match the extracted samples.')
        for values in (contraction_data,speed_data):
            values[:] = [(float(t),row[1]) for t,row in zip(times,values)]
        intervals = np.diff(times)
        median = float(np.median(intervals)) if len(intervals) else 1000/fps
        timing_segments = np.split(np.arange(len(times)),np.flatnonzero(intervals>1.5*median)+1)
        fps = 1000/median

    source_amplitudes=[row[1] for row in contraction_data]
    source_area=list(area_norm_data)
    source_fractional=list(fractional_data)
    # App jobs pass the settings snapshot recorded in their processing manifest.
    if kinetics_config is None:
        from tempo_config import tempo_preferences
        kinetics_config = tempo_preferences.get_category("kinetics")
    enable_asls = kinetics_config.get("enable_asls_detrending", {}).get("value", True)
    asls_lam = float(kinetics_config.get("asls_lambda", {}).get("value", 100000.0))
    asls_p = float(kinetics_config.get("asls_p", {}).get("value", 0.001))
    cutoff_hz = float(kinetics_config.get("lowpass_cutoff_hz", {}).get("value", 18.0))
    enable_lowpass = kinetics_config.get("enable_lowpass_filter", {}).get("value", True)
    savgol_window_pct = float(kinetics_config.get("savgol_window_pct", {}).get("value", 7))
    peak_prominence_pct = float(kinetics_config.get("peak_prominence_pct", {}).get("value", 30))

    # AsLS Baseline Detrending (anchored to diastolic valleys)
    if enable_asls and len(contraction_data)>10:
        segments=timing_segments if timing_segments is not None else [np.arange(len(contraction_data))]
        for indices in segments:
            if len(indices)<=10:continue
            raw_detrended,_=asls_baseline_detrend(np.asarray(source_amplitudes)[indices],lam=asls_lam,p=asls_p)
            area_detrended,_=asls_baseline_detrend(np.asarray(source_area)[indices],lam=asls_lam,p=asls_p)
            fractional_detrended,_=asls_baseline_detrend(np.asarray(source_fractional)[indices],lam=asls_lam,p=asls_p)
            for offset,index in enumerate(indices):
                contraction_data[index]=(contraction_data[index][0],float(raw_detrended[offset]))
                area_norm_data[index]=float(area_detrended[offset])
                fractional_data[index]=float(fractional_detrended[offset])

    # Pre-detect systolic peaks to guide directional anisotropy tracking
    y_sig = np.array([pt[1] for pt in contraction_data])
    distance = max(1, int(fps * 0.20))
    prom_thresh = (np.max(y_sig) - np.min(y_sig)) * peak_prominence_pct / 100.0 if len(y_sig) > 0 else 0.0
    pre_peaks, _ = find_peaks(y_sig, distance=distance, prominence=prom_thresh) if len(y_sig) > distance else ([], {})
    pre_left_ips = []
    if len(pre_peaks) > 0:
        _, _, left_ips, _ = peak_widths(y_sig, pre_peaks, rel_height=0.90)
        pre_left_ips = left_ips

    # Directional Anisotropy via Sparse Lucas-Kanade Vector Flow
    anisotropy_res = dict(Fractional_Anisotropy=float('nan'),Contraction_Angle_deg=float('nan'),Vector_Coherence=float('nan'),beat_metrics=[]) if timing_segments is not None else compute_directional_anisotropy(
        frames, polygon_mask=polygon_mask, fps=fps,
        peaks=pre_peaks, left_ips=pre_left_ips, cancel=cancel
    )
    beat_anisotropy = anisotropy_res.get("beat_metrics", [])
    
    if cancel:
        cancel.check()

    # Save Raw Data to CSV
    raw_csv_path = os.path.join(output_dir, f"{cell_id}_raw_kinematics.csv")
    with open(raw_csv_path, mode='w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Time (ms)", "Contraction (Raw a.u.)", "Contraction (Area-Norm a.u.)", "Contraction (Fractional dI/I0)", "Speed (a.u.)",
                         "Contraction (Source a.u.)","Area-Norm (Source a.u.)","Fractional (Source dI/I0)"])
        for i in range(len(contraction_data)):
            if cancel:
                cancel.check()
            speed_val = speed_data[i][1] if i < len(speed_data) else 0.0
            an_val = area_norm_data[i] if i < len(area_norm_data) else contraction_data[i][1]
            frac_val = fractional_data[i] if i < len(fractional_data) else 0.0
            writer.writerow([f"{contraction_data[i][0]:.9f}", f"{contraction_data[i][1]:.4f}", f"{an_val:.4f}", f"{frac_val:.4f}", f"{speed_val:.4f}",
                             source_amplitudes[i],source_area[i],source_fractional[i]])
            
    # Run Transient Analysis & Save Summary CSV
    metrics = [] if timing_segments is not None else analyze_transients(
        contraction_data, fps=fps,
        area_norm_data=area_norm_data,
        fractional_data=fractional_data,
        anisotropy_beat_metrics=beat_anisotropy,
        cutoff_hz=cutoff_hz, cancel=cancel, enable_lowpass=enable_lowpass,
        savgol_window_pct=savgol_window_pct, peak_prominence_pct=peak_prominence_pct
    )
    if timing_segments is not None:
        metrics=[]
        for indices in timing_segments:
            if len(indices)<5:
                continue
            original_times=np.asarray([contraction_data[i][0] for i in indices])
            uniform=np.arange(original_times[0],original_times[-1]+1e-6,1000/fps)
            if len(uniform)<5:
                continue
            def sampled(values):
                return np.interp(uniform,original_times,np.asarray(values)[indices]).tolist()
            samples=list(zip(uniform,sampled([row[1] for row in contraction_data])))
            segment_metrics=analyze_transients(samples,fps=fps,area_norm_data=sampled(area_norm_data),
                fractional_data=sampled(fractional_data),cutoff_hz=cutoff_hz,cancel=cancel,
                enable_lowpass=enable_lowpass,savgol_window_pct=savgol_window_pct,
                peak_prominence_pct=peak_prominence_pct)
            source_peaks=[int(np.argmin(abs(original_times-row['Time_of_Peak_ms']))) for row in segment_metrics]
            source_starts=[int(np.argmin(abs(original_times-(row['Time_of_Peak_ms']-row['CT90_ms'])))) for row in segment_metrics]
            flow=compute_directional_anisotropy(FrameSlice(frames,indices[0],indices[-1]+1),polygon_mask=polygon_mask,
                fps=fps,peaks=source_peaks,left_ips=source_starts,cancel=cancel)['beat_metrics'] if segment_metrics else []
            for position,row in enumerate(segment_metrics):
                evidence=flow[position] if position<len(flow) else {}
                for key in ('Fractional_Anisotropy','Contraction_Angle_deg','Vector_Coherence'):
                    row[key]=evidence.get(key,float('nan'))
                row['Peak_Number']=len(metrics)+1
                metrics.append(row)
    
    if metrics:
        summary_path = os.path.join(output_dir, f"{cell_id}_transients_summary.csv")
        with open(summary_path, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "Peak #", "Time of Peak (ms)", 
                "Peak Amplitude (a.u.)", 
                "Peak Amplitude (Area-Norm a.u.)", 
                "Peak Amplitude (Fractional dI/I0)", 
                "Time-to-Peak (ms)", "Relaxation Time (ms)", 
                "Transient Duration (ms)", 
                "CT50 (ms)", "CT90 (ms)", "RT50 (ms)", "RT90 (ms)", 
                "Max Contraction Velocity", "Max Relaxation Velocity", 
                "Peak-to-Peak Time (ms)",
                "Fractional Anisotropy", "Contraction Angle (deg)", "Vector Coherence"
            ])
            for m in metrics:
                writer.writerow([
                    m["Peak_Number"], 
                    f"{m['Time_of_Peak_ms']:.1f}", 
                    f"{m['Peak_Amplitude_Raw_au']:.4f}",
                    f"{m['Peak_Amplitude_AreaNorm_au']:.4f}",
                    f"{m['Peak_Amplitude_Fractional_dI_I0']:.4f}",
                    f"{m['Time_to_Peak_ms']:.1f}",
                    f"{m['Relaxation_Time_ms']:.1f}",
                    f"{m['Transient_Duration_ms']:.1f}",
                    f"{m['CT50_ms']:.1f}",
                    f"{m['CT90_ms']:.1f}",
                    f"{m['RT50_ms']:.1f}",
                    f"{m['RT90_ms']:.1f}",
                    f"{m['Max_Contraction_Velocity']:.4f}",
                    f"{m['Max_Relaxation_Velocity']:.4f}",
                    f"{m['Peak_to_Peak_Time_ms']:.1f}",
                    f"{m['Fractional_Anisotropy']:.4f}",
                    f"{m['Contraction_Angle_deg']:.1f}",
                    f"{m['Vector_Coherence']:.4f}"
                ])
        print(f"[VK ENGINE] Extracted {len(metrics)} peaks (Anisotropy α={anisotropy_res['Fractional_Anisotropy']:.3f}, θ={anisotropy_res['Contraction_Angle_deg']:.1f}°) - Saved to {os.path.basename(summary_path)}")
    else:
        print(f"[VK ENGINE] No distinct contraction peaks detected for {cell_id}.")
        
    return True
