"""Model filenames shared by packaging and training, without native imports."""

MODEL_FILES = {'nano_scout':'yolo26n-scout.pt', 'stardist':'stardist_nuclei.zip', 'cellpose':'cellpose4_cardio.pt'}
MODEL_LABELS = {'nano_scout': 'Nano Scout', 'stardist': 'StarDist Nuclei', 'cellpose': 'Cellpose Cells'}
MODEL_ROLES = {'nano_scout': 'scout', 'stardist': 'nuclei', 'cellpose': 'cells'}

# Explicit historical artifacts remain readable during release upgrades. Remote
# manifests never acquire the authority to name application or configuration files.
INSTALLABLE_MODEL_FILES = frozenset((*MODEL_FILES.values(), 'yolo26s-seg.pt', 'stardist_cardio.zip'))


def installed_model_dir(repo=None):
    """Isolate downloaded weights from application code and other workspaces."""
    import hashlib
    from pathlib import Path
    from tempo_config import get_config_dir, tempo_preferences
    repo = repo or tempo_preferences.get('collaboration', 'hf_models_repo', '')
    return Path(get_config_dir()).parent/'models'/hashlib.sha256(repo.encode()).hexdigest()[:20]


def local_model_path(filename):
    """Prefer verified installations, retaining existing bundled/local weights."""
    from pathlib import Path
    from tempo_config import get_base_dir
    if filename not in INSTALLABLE_MODEL_FILES:
        raise ValueError('Unknown TEMPO model filename.')
    installed = installed_model_dir()/filename
    return installed if installed.is_file() else Path(get_base_dir())/filename
