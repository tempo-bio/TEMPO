import os
import sys
import json
import math
import tempfile
import hashlib
from tempo_credentials import safe_profile_name

def get_base_dir():
    """Return the base directory of the application, taking PyInstaller bundle paths into account."""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

def get_config_dir():
    """Personal settings live outside the source and installed application."""
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
        return os.path.join(root, 'TEMPO', 'config')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'TEMPO', 'config')
    root = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(root, 'TEMPO', 'config')


def get_data_dir(value=None):
    """Resolve personal storage without creating or moving any user data."""
    if value is None:
        value = tempo_preferences.get('capture', 'data_folder', '')
    return os.path.abspath(os.path.expanduser(value or os.path.join('~', 'Documents', 'TEMPO')))


def get_collaboration_dir(repo=None):
    """Keep each dataset's images, reservations and unsynced drafts together."""
    repo = repo or tempo_preferences.get('collaboration', 'hf_dataset_repo', 'tempo-bio/cardiomyocyte-data')
    base = os.path.join(get_base_dir(), 'datasets')
    if repo == 'tempo-bio/cardiomyocyte-data':
        return os.path.join(base, 'universal_images')
    return os.path.join(base, 'workspaces', hashlib.sha256(repo.encode()).hexdigest()[:20], 'universal_images')


def get_legacy_config_path():
    # Bundled preferences must never become a new user's personal settings.
    if getattr(sys, 'frozen', False):
        return None
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config', 'tempo_preferences.json')


def migrate_preferences(payload):
    """Publish a complete legacy document without overwriting existing settings."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='tempo_preferences.', suffix='.tmp', dir=CONFIG_DIR)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, CONFIG_PATH)
        except FileExistsError:
            return False
        with open(CONFIG_PATH, 'rb') as stream:
            if stream.read() != payload:
                raise OSError('Migrated preferences could not be verified.')
        return True
    finally:
        if temporary is not None:
            os.unlink(temporary)

CONFIG_DIR = get_config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "tempo_preferences.json")

DEFAULT_PREFERENCES = {
    "appearance": {
        "theme": {
            "value": "dark",
            "options": ["dark", "light", "oatmeal"],
            "option_labels": {
                "dark": "Dark",
                "light": "Light",
                "oatmeal": "Oatmeal",
            },
            "label": "Theme",
            "description": "Visual palette for window surfaces, controls, and physiological plots.",
        },
        "animate_activity_icon": {
            "value": False,
            "label": "Animate Dock/taskbar icon while working",
        },
    },
    "capture": {
        "data_folder": {
            "value": "",
            "label": "Data Folder",
            "description": "New recordings and results are saved here. Existing files stay in their current locations.",
        },
        "mm_adapter_path": {
            "value": "",
            "label": "Micro-Manager Adapter Directory",
            "description": "Device adapter path discovered by graphical setup.",
        },
        "camera_profile": {
            "value": "FLIR_BFS_U3_16S2M_CS",
            "options": ["FLIR_BFS_U3_16S2M_CS", "Generic_MicroManager", "OpenCV_Webcam"],
            "label": "Camera Hardware Profile",
            "description": "Camera type used for acquisition settings.",
        },
        "panel_widths": {
            "value": [300, 650, 320],
            "label": "Acquisition panel widths",
        },
        "target_fps": {
            "value": 100,
            "min": 10,
            "max": 226,
            "label": "Acquisition Frame Rate (FPS)",
            "description": "Requested frames per second.",
        },
        "bit_depth": {
            "value": "Mono8",
            "options": ["Mono8", "Mono16"],
            "label": "Sensor Bit Depth Mode",
            "description": "Grayscale precision of captured images: 8 or 16 bits per pixel.",
        },
        "circular_buffer_mb": {
            "value": 4096,
            "min": 512,
            "max": 16384,
            "label": "Micro-Manager Buffer Allocation (MB)",
            "description": "Memory reserved for camera frames waiting to be processed.",
        },
        "pre_trigger_buffer_ms": {
            "value": 500,
            "min": 0,
            "max": 2000,
            "label": "Pre-Trigger Buffer (ms)",
            "description": "Amount of video retained before a recording trigger.",
        },
        "overlap_percentage": {
            "value": 25,
            "min": 10,
            "max": 40,
            "label": "Image Overlap Guide (%)",
            "description": "Image overlap shown by the guide when moving between fields of view.",
        }
    },
    "annotator": {
        "confidence_threshold": {
            "value": 0.15,
            "min": 0.05,
            "max": 0.90,
            "step": 0.05,
            "label": "Scout Confidence Threshold",
            "description": "Lower values detect more cells but may include more false detections.",
        },
    },
    "scout": {
        "settle_dwell_ms": {
            "value": 40,
            "min": 10,
            "max": 200,
            "label": "Wait After Stage Movement (ms)",
            "description": "Wait after moving the stage before capturing an image, allowing vibration to settle.",
        },
        "live_threshold": {
            "value": 3,
            "min": 1,
            "max": 50,
            "label": "Minimum Live Cells for Recording",
            "description": "Record a field when it contains at least this many healthy individual cells. Groups do not independently qualify a field.",
        },
        "scout_duration_sec": {
            "value": 15.0,
            "min": 3.0,
            "max": 60.0,
            "label": "Field Recording Duration (seconds)",
            "description": "Duration of each recording started by plate scouting.",
        },
        "default_traversal": {
            "value": "All 96 Wells (Serpentine)",
            "options": ["All 96 Wells (Serpentine)", "Row A Only (12 wells)", "Rows A-B (24 wells)", "Rows A-D (48 wells)"],
            "label": "Default Stage Traversal Pattern",
            "description": "Wells to visit and their order during a plate scan.",
        }
    },
    "cruncher": {
        "in_memory_streaming": {
            "value": True,
            "label": "Process Videos in Memory",
            "description": "Process cell frames without saving intermediate video files.",
        },
        "export_debug_avis": {
            "value": False,
            "label": "Save Intermediate Cell Videos",
            "description": "Save each masked cell video for inspection in a video viewer or analysis tool.",
        },
        "ref_frame_search_window": {
            "value": 300,
            "min": 50,
            "max": 1000,
            "label": "Reference Frame Search Window (Frames)",
            "description": "Number of initial frames searched for a low-motion reference image.",
        }
    },
    "analyzer": {
        "default_metric": {
            "value": "Peak_Amplitude_AreaNorm_au",
            "options": [
                "Peak_Amplitude_AreaNorm_au",
                "Peak_Amplitude_Fractional_dI_I0",
                "Peak_Amplitude_au",
                "Time_to_Peak_ms",
                "Peak_to_Peak_Time_ms",
                "CT50_ms",
                "Max_Contraction_Velocity"
            ],
            "label": "Default Map Metric",
            "option_labels": {"Peak_Amplitude_AreaNorm_au": "Area-normalized amplitude", "Peak_Amplitude_Fractional_dI_I0": "Fractional intensity change", "Peak_Amplitude_au": "Amplitude (a.u.)", "Time_to_Peak_ms": "Time to peak (ms)", "Peak_to_Peak_Time_ms": "Beat interval (ms)", "CT50_ms": "Contraction time, 50% (ms)", "Max_Contraction_Velocity": "Contraction intensity rate (a.u./ms)"},
            "description": "Measurement used to color the cells on the spatial map.",
        },
        "colormap_palette": {
            "value": "viridis",
            "options": ["viridis", "plasma", "inferno", "magma"],
            "label": "Color Map Palette",
            "description": "Color scale used for measurements on the cell map.",
        }
    },
    "kinetics": {
        "enable_asls_detrending": {
            "value": True,
            "label": "Asymmetric Least Squares (AsLS) Baseline Detrending",
            "description": "Subtracts an estimated slowly varying baseline from the signal.",
        },
        "asls_lambda": {
            "value": 100000.0,
            "min": 1000.0,
            "max": 10000000.0,
            "label": "AsLS Smoothing Regularizer (Lambda)",
            "description": "Controls the stiffness of the fitted baseline curve.",
        },
        "asls_p": {
            "value": 0.001,
            "decimals": 4,
            "step": 0.0001,
            "min": 0.0001,
            "max": 0.05,
            "label": "AsLS Asymmetry Ratio (p)",
            "description": "Controls how strongly values above the estimated baseline affect the baseline fit.",
        },
        "enable_lowpass_filter": {
            "value": True,
            "label": "Butterworth Low-Pass Denoising Filter",
            "description": "Attenuates signal components above the cutoff frequency.",
        },
        "lowpass_cutoff_hz": {
            "value": 18.0,
            "min": 5.0,
            "max": 50.0,
            "label": "Low-Pass Cutoff Frequency (Hz)",
            "description": "Signal frequencies above this cutoff are attenuated. Lower values can also smooth rapid changes in the measured signal.",
        },
        "savgol_window_pct": {
            "value": 7,
            "min": 3,
            "max": 20,
            "label": "Intensity Rate Smoothing Window (% of FPS)",
            "description": "Window size for estimating the rate of intensity change. Larger windows smooth more detail.",
        },
        "peak_prominence_pct": {
            "value": 30,
            "min": 10,
            "max": 60,
            "label": "Peak Prominence Detection Threshold (%)",
            "description": "Minimum peak prominence as a percentage of the signal range. Higher values exclude smaller peaks.",
        }
    },
    "pacing": {
        "amplitude_v": {
            "value": 0.6,
            "min": 0.0,
            "max": 1.0,
            "step": 0.05,
            "label": "Requested Stimulation Voltage (V)",
            "description": "Default DAC voltage request for a new well session.",
        },
        "pulse_width_ms": {
            "value": 2.0,
            "min": 0.1,
            "max": 9999.0,
            "step": 0.1,
            "label": "Monophasic Pulse Width (ms)",
            "description": "Requested duration of each pacing pulse.",
        },
        "pacing_frequency_hz": {
            "value": 1.0,
            "min": 0.1,
            "max": 100.0,
            "step": 0.1,
            "label": "Pacing Frequency (Hz)",
            "description": "Requested pulses per second. A setting of 1 Hz requests 60 pulses per minute.",
        }
    },
    "collaboration": {
        "colab_notebook_url": {
            "value": "",
            "label": "Colab Notebook",
            "description": "The reusable Colab notebook connected to the Maintainer cloud-training workspace.",
        },
        "is_maintainer": {
            "value": False,
            "label": "Maintainer Override",
            "description": "Explicit local maintainer designation; an authenticated write token is still required.",
        },
        "hf_org_id": {
            "value": "tempo-bio",
            "label": "Hugging Face Organization",
            "description": "Organization hosting shared images and annotations. Published models may use a separate read-only organization.",
        },
        "hf_dataset_repo": {
            "value": "tempo-bio/cardiomyocyte-data",
            "label": "Private Dataset Repository",
            "description": "Private repository containing shared images and annotations.",
        },
        "hf_models_repo": {
            "value": "tempo-bio-settings/tempo-models",
            "label": "Model Hub Repository",
            "description": "Repository containing published model releases.",
        },
        "collaborator_id": {
            "value": "default_collaborator",
            "label": "Profile / Lab Name",
            "description": "Display name for your profile or laboratory. Reservations and submissions belong to your verified account.",
        },
        "lock_ttl_days": {
            "value": 7,
            "min": 7,
            "max": 7,
            "label": "Reservation Duration (Days)",
            "description": "Images become available to another collaborator when the reservation expires.",
        }
    }
}

class TempoConfig:
    """Centralized singleton managing user preferences and hardware settings across TEMPO."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(TempoConfig, cls).__new__(cls)
            cls._instance._preferences = {}
            cls._instance.load()
        return cls._instance

    @staticmethod
    def _saved_value(value, default):
        if isinstance(default, bool):
            if type(value) is not bool:
                raise ValueError('Expected a Boolean.')
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Expected a finite number.')
            if isinstance(default, int):
                if value != int(value):
                    raise ValueError('Expected a whole number.')
                value = int(value)
            else:
                value = float(value)
        elif isinstance(default, list):
            if not isinstance(value, list) or len(value) != len(default):
                raise ValueError(f'Expected {len(default)} panel widths.')
            value = [TempoConfig._saved_value(item, template) for item, template in zip(value, default)]
            if any(item <= 0 for item in value):
                raise ValueError('Panel widths must be positive.')
        elif not isinstance(value, str):
            raise ValueError('Expected text.')
        return value

    def load(self):
        """Loads preferences from disk, merging with defaults if missing."""
        import copy
        self._preferences = copy.deepcopy(DEFAULT_PREFERENCES)
        self._pending_workspace = None
        self.load_error = ''
        source = CONFIG_PATH
        legacy = get_legacy_config_path()
        if not os.path.lexists(source) and legacy and os.path.exists(legacy):
            source = legacy

        if os.path.lexists(source):
            try:
                with open(source, 'rb') as f:
                    payload = f.read()
                saved = json.loads(payload)
                if not isinstance(saved, dict):
                    raise ValueError('Saved settings must contain named categories.')
                candidate = copy.deepcopy(self._preferences)
                for cat, items in saved.items():
                    if cat in candidate:
                        if not isinstance(items, dict):
                            raise ValueError(f'Invalid settings category: {cat}.')
                        for k, v in items.items():
                            if k in candidate[cat]:
                                if not isinstance(v, dict):
                                    raise ValueError(f'Invalid saved setting: {cat}.{k}.')
                                if 'value' not in v:
                                    continue
                                value, default = v['value'], candidate[cat][k]['value']
                                try:
                                    candidate[cat][k]['value'] = self._saved_value(value, default)
                                    if (cat, k) == ('collaboration', 'collaborator_id'):
                                        candidate[cat][k]['value'] = safe_profile_name(value)
                                except ValueError as exc:
                                    raise ValueError(f'Invalid saved value: {cat}.{k}. {exc}') from exc
                self._preferences = candidate
            except Exception as e:
                self.load_error = str(e)
                print(f"[CONFIG] Warning: Failed to load settings ({e}). Using defaults.")
                return
            if source != CONFIG_PATH:
                try:
                    if not migrate_preferences(payload):
                        self.load()  # An existing private file wins a concurrent migration.
                except Exception as exc:
                    self.load_error = 'Settings could not be moved to your user folder. Your existing settings have been retained. ' + str(exc)

    def save(self, preferences=None):
        """Saves current preferences to disk."""
        temporary = None
        try:
            document = self._preferences if preferences is None else preferences
            if preferences is None and getattr(self, '_pending_workspace', None):
                import copy
                document = copy.deepcopy(document)
                for key, value in self._pending_workspace.items():
                    document['collaboration'][key]['value'] = value
            profile = document.get('collaboration', {}).get('collaborator_id', {}).get('value')
            if profile and safe_profile_name(profile) != profile:
                raise ValueError('Access tokens and account IDs cannot be saved as profile names.')
            os.makedirs(CONFIG_DIR, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='tempo_preferences.', suffix='.tmp', dir=CONFIG_DIR)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(document, stream, indent=4, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, CONFIG_PATH)
            self.load_error = ''
            return True
        except Exception as e:
            print(f"[CONFIG] Error: Failed to save preferences to {CONFIG_PATH}: {e}")
            return False
        finally:
            if temporary is not None and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError as exc:
                    print(f'[CONFIG] Warning: Could not remove incomplete settings file {temporary}: {exc}')

    def reset_defaults(self):
        """Resets all preferences back to factory defaults."""
        import copy
        self._preferences = copy.deepcopy(DEFAULT_PREFERENCES)
        return self.save()

    def get(self, category, key, default=None):
        """Returns the current setting value."""
        try:
            return self._preferences[category][key]["value"]
        except KeyError:
            return default

    def set(self, category, key, value):
        """Set an in-memory value; save() persists it in the user folder."""
        if (category, key) == ('collaboration', 'collaborator_id') and value and safe_profile_name(value) != value:
            raise ValueError('Access tokens and account IDs cannot be used as profile names.')
        if category in self._preferences and key in self._preferences[category]:
            self._preferences[category][key]["value"] = value
            return True
        return False

    def get_category(self, category):
        """Returns all configuration dictionary items for a tab category."""
        return self._preferences.get(category, {})

    def get_all(self):
        """Returns the full preferences dictionary across all categories."""
        return self._preferences

    def save_to_disk(self, preferences=None):
        """Alias for save()."""
        return self.save(preferences)

    def reset_to_defaults(self):
        """Alias for reset_defaults()."""
        return self.reset_defaults()

# Global access point
config = TempoConfig()
tempo_preferences = config
