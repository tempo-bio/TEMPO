"""Administrator-published workspace settings, read from a stable trusted location."""
import copy
from datetime import datetime, timezone
import json
import re
import os
import sys
from urllib.parse import urlparse

from huggingface_hub import CommitOperationAdd, hf_hub_download
from huggingface_hub.errors import EntryNotFoundError
from tempo_config import tempo_preferences

# Discovery must survive an administrator changing the dataset/model destinations.
# Keep this bootstrap location independent of editable workspace preferences.
# Its private organization must grant collaborators read-only access and reserve
# all write access for organization admins. See docs/WORKSPACE_ADMINISTRATION.md.
CONTROL_REPO = 'tempo-bio-settings/workspace'
CONTROL_FILE = 'tempo_shared_settings.json'
SHARED_KEYS = ('colab_notebook_url', 'hf_org_id', 'hf_dataset_repo', 'hf_models_repo')
REPOSITORY_KEYS = ('hf_org_id', 'hf_dataset_repo', 'hf_models_repo')


def values(preferences=None):
    preferences = preferences or tempo_preferences
    return {key: preferences.get('collaboration', key, '') for key in SHARED_KEYS}


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) != set(SHARED_KEYS):
        raise ValueError('Shared settings must contain the notebook, organization, dataset and model repository.')
    if any(not isinstance(value, str) for value in settings.values()):
        raise ValueError('Shared settings must contain text values.')
    result = {key: value.strip() for key, value in settings.items()}
    name = r'[A-Za-z0-9][A-Za-z0-9_.-]*'
    if not re.fullmatch(name, result['hf_org_id']):
        raise ValueError('Enter a valid Hugging Face organization.')
    for key in ('hf_dataset_repo', 'hf_models_repo'):
        if not re.fullmatch(name + '/' + name, result[key]):
            raise ValueError('Repository names must use organization/repository format.')
    if result['hf_dataset_repo'].split('/')[0] != result['hf_org_id']:
        raise ValueError('The dataset repository must belong to the shared data organization.')
    link = result['colab_notebook_url']
    parsed = urlparse(link)
    if link and (len(link) > 2048 or parsed.scheme != 'https' or parsed.hostname != 'colab.research.google.com'
                 or parsed.username or parsed.password or not parsed.path.startswith(('/drive/', '/github/'))):
        raise ValueError('Enter a Colab notebook URL, or leave the notebook link empty.')
    if any('hf_' in value or 'hf:' in value or len(value) > 2048 for value in result.values()):
        raise ValueError('Shared settings cannot contain access tokens or account IDs.')
    return result


def is_workspace_admin(hub):
    """Use the trusted repository owner, never a local role or editable destination."""
    hub._online()
    token = hub.token
    who = hub.api.whoami(token=token)
    owner = CONTROL_REPO.split('/')[0]
    allowed = any(
        org.get('name') == owner and org.get('roleInOrg') == 'admin' for org in who.get('orgs', []))
    if token != hub.token:
        raise PermissionError('The account changed during administrator verification.')
    return bool(allowed)


def _control_revision(hub, token):
    info = hub.api.repo_info(repo_id=CONTROL_REPO, repo_type='model', token=token)
    if info.private is not True:
        raise PermissionError('The shared settings repository must be private. Saved settings were retained.')
    if not isinstance(info.sha, str) or not info.sha:
        raise ValueError('The shared settings repository has no readable revision.')
    return info.sha


def _read(hub, revision, *, missing_ok=False):
    try:
        path = hf_hub_download(repo_id=CONTROL_REPO, repo_type='model', filename=CONTROL_FILE,
                               revision=revision, token=hub.token, etag_timeout=3)
        with open(path, encoding='utf-8') as stream:
            document = json.load(stream)
    except EntryNotFoundError:
        if missing_ok:
            return None
        raise
    if not isinstance(document, dict) or document.get('schema_version') != 2:
        raise ValueError('The shared settings file has an unsupported format. Saved settings were retained.')
    validate_settings(document.get('settings'))
    if not isinstance(document.get('updated_by'), str) or not re.fullmatch(r'hf:[A-Za-z0-9_-]+', document['updated_by']):
        raise ValueError('The shared settings file has no valid publisher record.')
    # The repository ACL is the trust boundary. updated_by is a private audit
    # field, not a claim that can authorize a writer. A data-repository writer
    # must never receive write access to this separate settings organization.
    return document


def fetch_shared_settings(hub):
    hub._online()
    token = hub.token
    revision = _control_revision(hub, token)
    document = _read(hub, revision, missing_ok=True)
    if token != hub.token:
        raise PermissionError('The account changed while shared settings were loading.')
    return None if document is None else dict(settings=validate_settings(document['settings']), revision=revision)


def apply_shared_settings(record, *, live=False):
    """Preserve personal preferences and defer live destination changes to restart."""
    if record is None:
        return False
    settings = validate_settings(record['settings'])
    previous = values()
    if settings == previous and not getattr(tempo_preferences, '_pending_workspace', None):
        return False
    candidate = copy.deepcopy(tempo_preferences.get_all())
    for key, value in settings.items():
        candidate['collaboration'][key]['value'] = value
    if not tempo_preferences.save_to_disk(candidate):
        raise OSError('Shared settings could not be saved. The previous settings remain in use.')
    restart = live and any(previous[key] != settings[key] for key in REPOSITORY_KEYS)
    tempo_preferences._pending_workspace = dict(settings) if restart else None
    for key, value in settings.items():
        if not restart or key not in REPOSITORY_KEYS:
            tempo_preferences.set('collaboration', key, value)
    if not live and previous['hf_dataset_repo'] != settings['hf_dataset_repo']:
        from tempo_config import get_collaboration_dir
        directory = get_collaboration_dir()
        hub_module = sys.modules.get('tempo_hub')
        if hub_module is not None:
            hub_module.UNIVERSAL_DIR = directory
            hub_module.LOCAL_LOCK_CACHE = os.path.join(directory, 'lock_manifest_cache.json')
            hub_module.LOCAL_SESSION_PATH = os.path.join(directory, 'local_session.json')
        backend = sys.modules.get('tempo_backend')
        if backend is not None:
            backend.UNIVERSAL_DATASET_DIR = directory
        os.makedirs(directory, exist_ok=True)
    return restart


def publish_shared_settings(hub, settings, *, previous=None):
    from tempo_hub import commit_and_confirm
    settings = validate_settings(settings)
    if not is_workspace_admin(hub):
        raise PermissionError('Only the workspace administrator can publish shared settings.')
    token = hub.token
    hub.api.auth_check(repo_id=CONTROL_REPO, repo_type='model', token=token, write=True)
    dataset = hub.api.repo_info(repo_id=settings['hf_dataset_repo'], repo_type='dataset', token=token)
    if not dataset.private:
        raise ValueError('The microscopy dataset repository must be private.')
    hub.api.auth_check(repo_id=settings['hf_dataset_repo'], repo_type='dataset', token=token, write=True)
    hub.api.repo_info(repo_id=settings['hf_models_repo'], repo_type='model', token=token)
    actor = hub.authenticated_account()
    for attempt in range(4):
        revision = _control_revision(hub, token)
        old = _read(hub, revision, missing_ok=True)
        current = validate_settings(old['settings']) if old is not None else None
        if current == settings:
            return dict(settings=settings, revision=revision)
        if previous is not None and current not in (None, previous):
            raise ValueError('Shared settings changed remotely. Refresh them before publishing your edits.')
        document = dict(schema_version=2, settings=settings, updated_by=actor,
                        updated_at=datetime.now(timezone.utc).isoformat())
        if token != hub.token:
            raise PermissionError('The account changed before shared settings could be published.')
        try:
            commit_and_confirm(hub.api, repo_id=CONTROL_REPO, repo_type='model', token=token,
                               parent_commit=revision,
                               operations=[CommitOperationAdd(path_in_repo=CONTROL_FILE,
                                                              path_or_fileobj=json.dumps(document).encode())],
                               commit_message='Update TEMPO shared workspace settings',
                               read_json=lambda name, rev: _read(hub, rev))
        except Exception as error:
            if getattr(getattr(error, 'response', None), 'status_code', None) in (409, 412) and attempt < 3:
                continue
            raise
        return dict(settings=settings, revision=revision)
    raise RuntimeError('Shared settings changed repeatedly. Refresh and retry.')
