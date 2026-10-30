"""Revision-pinned Hugging Face collaboration and verified model distribution."""
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ['HF_HUB_DISABLE_XET'] = '1'
try:
    from huggingface_hub import HfApi, hf_hub_download, CommitOperationAdd
    from huggingface_hub.errors import EntryNotFoundError
    HAS_HF = True
except ImportError:
    HAS_HF = False

from tempo_config import tempo_preferences, get_collaboration_dir
from tempo_credentials import safe_profile_name, contains_access_token, redact_credentials
from tempo_secure_store import load_hf_token, CredentialStorageError
from tempo_annotation_store import read_session, update_session
from tempo_annotations import (validate_annotations, image_filename, canonical_image_name,
                               merge_image_records, reconcile_image_identities)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UNIVERSAL_DIR = get_collaboration_dir()
LOCAL_LOCK_CACHE = os.path.join(UNIVERSAL_DIR, 'lock_manifest_cache.json')
LOCAL_SESSION_PATH = os.path.join(UNIVERSAL_DIR, 'local_session.json')
_desktop_http_configured = False


def _desktop_request_timeout(request):
    # Some HfApi methods explicitly pass timeout=None, overriding the client.
    # Preserve finite SDK download limits while bounding every unbounded phase.
    limits = {'connect': 10, 'read': 30, 'write': 30, 'pool': 30}
    current = request.extensions.get('timeout', {})
    request.extensions['timeout'] = {
        phase: current[phase] if current.get(phase) is not None else limit
        for phase, limit in limits.items()}


def _desktop_http_client():
    # Keep the SDK's offline guard, tracing hooks, redirects and TLS defaults.
    from huggingface_hub.utils._http import default_client_factory
    client = default_client_factory()
    # Use the SDK client's own timeout type: Hub 1 uses httpx, Hub 2 httpx2.
    client.timeout = type(client.timeout)(30, connect=10)
    client.event_hooks['request'].append(_desktop_request_timeout)
    return client


def configure_desktop_http():
    """Bound inactive desktop requests without changing Colab's upload policy."""
    global _desktop_http_configured
    if HAS_HF and not _desktop_http_configured:
        from huggingface_hub import set_client_factory
        set_client_factory(_desktop_http_client)
        _desktop_http_configured = True


def atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _install_image_copy(source, target, expected_digest, *, optional_alias=False):
    """Publish a complete copy without replacing a local annotation input."""
    def existing():
        if Path(target).is_file() and TempoHub.calculate_sha256(target) == expected_digest:
            return True
        if optional_alias:
            return False
        raise FileExistsError(f'Existing local image {Path(target).name} differs from this download and was preserved. '
                              'Resolve the filename conflict before retrying checkout.')

    if os.path.lexists(target):
        return existing()
    fd, temporary = tempfile.mkstemp(prefix='.image-download-', suffix='.tmp', dir=Path(target).parent)
    os.close(fd)
    try:
        shutil.copy2(source, temporary)
        if TempoHub.calculate_sha256(temporary) != expected_digest:
            raise ValueError('Image changed during copying; no local image was installed.')
        with open(temporary, 'r+b') as stream:
            os.fsync(stream.fileno())
        try:
            # A same-directory hard link publishes the complete file atomically
            # and fails if another process created the destination first.
            os.link(temporary, target)
        except FileExistsError:
            return existing()
        return True
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _read_checkout_session():
    return read_session(LOCAL_SESSION_PATH)


def _model_records(manifest):
    """Validate the complete release using portable, case-insensitive names."""
    from tempo_model_catalog import INSTALLABLE_MODEL_FILES
    if not isinstance(manifest, dict) or not isinstance(manifest.get('models', {}), dict):
        raise ValueError('Invalid model release manifest.')
    models, identities = {}, set()
    for name, record in manifest.get('models', {}).items():
        if (not isinstance(name, str) or name not in INSTALLABLE_MODEL_FILES
                or any(c in name for c in '/\\:<>"|?*')
                or any(ord(c) < 32 for c in name) or name[-1] in '. '
                or name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}
                or re.fullmatch(r'(COM|LPT)[1-9]', name.split('.')[0].upper())
                or name.casefold() in identities or not isinstance(record, dict)
                or not isinstance(record.get('sha256'), str)
                or not re.fullmatch('[0-9a-fA-F]{64}', record['sha256'])):
            raise ValueError('Invalid or conflicting model release filenames/checksums.')
        identities.add(name.casefold())
        models[name] = dict(record, sha256=record['sha256'].lower())
    if models and (not isinstance(manifest.get('revision'), str) or not manifest['revision'].strip()):
        raise ValueError('A pinned model release revision is required.')
    return models


def _lease_expiry(record):
    value = record['expires_at']
    if not isinstance(value, str):
        raise ValueError('Lease expiry must be a timestamp.')
    expiry = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    if expiry.tzinfo is None:
        raise ValueError('Lease expiry must include a timezone.')
    return expiry


def expired(record):
    try:
        return _lease_expiry(record) <= datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        return True


def validate_lock_manifest(manifest):
    """Malformed reservations cannot become editable cached state or a write base."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get('images'), dict):
        raise ValueError('Malformed reservation manifest: expected named image records.')
    if type(manifest.get('version', 1)) is not int or manifest.get('version', 1) not in (1, 2, 3):
        raise ValueError('Unsupported image identity manifest version.')
    profiles = manifest.get('collaborators', {})
    if not isinstance(profiles, dict) or any(not isinstance(actor, str) or not isinstance(profile, dict)
                                             for actor, profile in profiles.items()):
        raise ValueError('Malformed collaborator display-name directory.')
    for profile in profiles.values():
        for key in ('profile_name', 'account_name'):
            if key in profile and (not isinstance(profile[key], str) or safe_profile_name(profile[key]) != profile[key]):
                raise ValueError('Invalid collaborator display name. Account IDs and access tokens cannot be display names.')
    for name, record in manifest['images'].items():
        if (not isinstance(name, str) or not name or not isinstance(record, dict)
                or record.get('status') not in ('available', 'locked', 'submitted', 'approved')):
            raise ValueError('Malformed reservation manifest: invalid image status record.')
        image_filename(name)
        if record['status'] in ('locked', 'submitted'):
            if not isinstance(record.get('claimed_by'), str) or not record['claimed_by'].strip():
                raise ValueError('Malformed reservation manifest: a reservation needs its owner.')
            try:
                _lease_expiry(record)
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError('Malformed reservation manifest: invalid lease expiry.') from exc
    if manifest.get('version', 1) >= 2:
        identities = set()
        for name, record in manifest['images'].items():
            digest = record.get('sha256')
            if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                    or record.get('image_id') != 'sha256:'+digest or digest in identities):
                raise ValueError('Malformed image identity index: missing or duplicate content identity.')
            identities.add(digest)
            image_filename(record.get('remote_filename'))
        aliases = manifest.get('aliases', {})
        if not isinstance(aliases, dict):
            raise ValueError('Malformed image identity aliases.')
        for alias, canonical in aliases.items():
            image_filename(alias)
            if not isinstance(canonical, str) or canonical not in manifest['images'] or alias in manifest['images']:
                raise ValueError('Image aliases must point directly to a canonical image.')
    return manifest


def commit_and_confirm(api, *, repo_id, repo_type, operations, token=None,
                       parent_commit=None, commit_message, read_json=None):
    """Resolve a lost acknowledgement only when every intended file is present.

    Never repeat an ambiguous write. Compare the complete intended payload at one
    authoritative remote revision; a failed read or mismatch preserves the error.
    Conflicts retain their normal optimistic-concurrency retry behavior.
    """
    expected = {}
    for operation in operations:
        value = operation.path_or_fileobj
        if isinstance(value, bytes):
            digest = hashlib.sha256(value).hexdigest()
            structured = json.loads(value) if operation.path_in_repo.endswith('.json') else None
        else:
            digest = TempoHub.calculate_sha256(value)
            structured = json.loads(Path(value).read_text()) if operation.path_in_repo.endswith('.json') else None
        expected[operation.path_in_repo] = (digest, structured)
    try:
        return api.create_commit(repo_id=repo_id, repo_type=repo_type, token=token,
                                 parent_commit=parent_commit, operations=operations,
                                 commit_message=commit_message)
    except Exception as error:
        if getattr(getattr(error, 'response', None), 'status_code', None) in (409, 412):
            raise
        try:
            revision = api.repo_info(repo_id=repo_id, repo_type=repo_type, token=token).sha
            # Read the manifest first to reject unapplied/changed writes cheaply.
            names = sorted(expected, key=lambda name: (not name.endswith('manifest.json'), name))
            for name in names:
                digest, structured = expected[name]
                if read_json is not None and name.endswith('.json'):
                    if read_json(name, revision) != structured:
                        raise ValueError('Remote JSON differs from the intended write.')
                else:
                    path = hf_hub_download(repo_id=repo_id, filename=name, revision=revision,
                                           repo_type=repo_type, token=token)
                    if TempoHub.calculate_sha256(path) != digest:
                        raise ValueError('Remote file differs from the intended write.')
        except Exception:
            raise error from None
        print('Remote write confirmed after an interrupted acknowledgement.', flush=True)
        return None


class TempoHub:
    def __init__(self, token=None):
        # Native-store access happens in a background operation, never on import.
        self._credential_lock = threading.RLock()
        self._set_credentials(token)
        self._credentials_ready = token is not None
        self.credential_error = ''

    @property
    def token(self):
        return self.__dict__.get('token')

    @token.setter
    def token(self, token):
        # Explicit credentials (including Colab secrets) live only in memory.
        self.__dict__['token'] = token
        self._account_id = None
        self._identity_token = None
        self._account_name = None
        if not getattr(self, 'api', None):
            self.api = HfApi(token=token or False) if HAS_HF else None
        self._credentials_ready = True
        self.credential_error = ''

    def _set_credentials(self, token):
        self.__dict__['token'] = token
        self.api = HfApi(token=token or False) if HAS_HF else None
        self._account_id = None
        self._identity_token = None
        self._account_name = None

    def _init_credentials(self, *, migrate=False):
        with self._credential_lock:
            self._load_credentials(migrate=migrate)

    def _load_credentials(self, *, migrate=False):
        try:
            token = load_hf_token(migrate=migrate) if HAS_HF else None
        except CredentialStorageError as exc:
            self._set_credentials(None)
            self._credentials_ready = False
            self.credential_error = str(exc)
            raise
        self._set_credentials(token)
        self._credentials_ready = True
        self.credential_error = ''

    def ensure_credentials(self, *, migrate=False):
        with self._credential_lock:
            if not self._credentials_ready:
                self._init_credentials(migrate=migrate)

    def authenticated_account(self, *, force_refresh=False):
        """Bind ownership to the immutable Hub account ID, never a lab label."""
        self._online()
        if not force_refresh and getattr(self, '_identity_token', None) == self.token and getattr(self, '_account_id', None):
            return self._account_id
        token = self.token
        who = self.api.whoami(token=token)
        if token != self.token:
            raise PermissionError('The account changed during verification. Verify the current account again.')
        identifier = who.get('id') if isinstance(who, dict) else None
        if not isinstance(identifier, str) or not identifier.strip() or contains_access_token(identifier, token):
            raise ValueError('The connected account could not be identified. Reconnect your account before editing shared data.')
        self._account_id = 'hf:'+identifier
        self._account_name = safe_profile_name(who.get('name'), token) or 'Verified account'
        self._identity_token = self.token
        return self._account_id

    def _require_actor(self, requested=None):
        actor = self.authenticated_account()
        if isinstance(requested, str) and requested.startswith('hf:') and requested != actor:
            raise PermissionError('The connected account changed. Refresh reservations before continuing.')
        return actor

    def get_dataset_repo(self):
        return tempo_preferences.get('collaboration', 'hf_dataset_repo', 'tempo-bio/cardiomyocyte-data')

    def get_models_repo(self):
        return tempo_preferences.get('collaboration', 'hf_models_repo', 'tempo-bio-settings/tempo-models')

    def get_collaborator_id(self):
        if getattr(self, '_identity_token', None) == self.token and getattr(self, '_account_id', None):
            return self._account_id
        return 'unverified_account'

    def get_account_display_name(self):
        if getattr(self, '_identity_token', None) != self.token or not getattr(self, '_account_id', None):
            return 'Account not verified'
        return safe_profile_name(getattr(self, '_account_name', None), self.token) or 'Verified account'

    def profile_name(self):
        name = safe_profile_name(tempo_preferences.get('collaboration', 'collaborator_id'), self.token)
        return name if name and name != 'default_collaborator' else (
            safe_profile_name(getattr(self, '_account_name', None), self.token) or 'Unnamed collaborator')

    def display_collaborator(self, actor, *, record=None, profiles=None):
        """Display labels never replace the immutable account used for ownership."""
        if not actor:
            return 'Unassigned'
        if actor == self.get_collaborator_id() and actor != 'unverified_account':
            return self.profile_name()
        profile = profiles.get(actor, {}) if isinstance(profiles, dict) else {}
        if not isinstance(profile, dict):
            profile = {}
        for name in (profile.get('profile_name'), (record or {}).get('profile_name'),
                     (record or {}).get('claimed_by_name'), profile.get('account_name'), actor):
            label = safe_profile_name(name, self.token)
            if label and label != 'default_collaborator':
                return label
        return 'Unnamed collaborator'

    def publish_collaborator_profile(self):
        self.authenticated_account()
        return self._transaction(lambda manifest, revision: ({}, True), 'Update collaborator display name',
                                 reconcile_images=False, skip_unchanged=True)

    def _prepare_manifest(self, manifest, revision):
        """Read one pinned snapshot; upgrade legacy identities without losing data."""
        master = self._read('universal_annotations.json', revision, {})
        original = copy.deepcopy(manifest)
        if manifest.get('version', 1) < 2:
            hashes = {}
            try:
                entries = list(self.api.list_repo_tree(repo_id=self.get_dataset_repo(), repo_type='dataset',
                                                      path_in_repo='images', revision=revision, token=self.token or False))
            except EntryNotFoundError:
                if manifest['images'] or master:
                    raise
                entries = []
            for entry in entries:
                if not hasattr(entry, 'blob_id'):
                    continue
                name = entry.path.removeprefix('images/') if hasattr(str, 'removeprefix') else entry.path[len('images/'):]
                image_filename(name)
                digest = entry.lfs.sha256 if entry.lfs else None
                if not digest:
                    path = hf_hub_download(repo_id=self.get_dataset_repo(), repo_type='dataset',
                                           filename=entry.path, revision=revision, token=self.token or False)
                    digest = self.calculate_sha256(path)
                hashes[name] = digest
            manifest = reconcile_image_identities(manifest, master, hashes)
        else:
            manifest = copy.deepcopy(manifest)
        master_clean = merge_image_records(master, manifest)
        drafts = self._read('annotation_drafts.json', revision, {})
        drafts_clean = merge_image_records(drafts, manifest)
        for name, record in master_clean.items():
            if name not in manifest['images']:
                raise ValueError(f'{name}: annotations are missing their verified image identity.')
            if record.get('image_id') not in (None, manifest['images'][name]['image_id']):
                raise ValueError(f'{name}: master annotations identify a different image.')
            record['image_id'] = manifest['images'][name]['image_id']
            if record.get('status') == 'approved':
                manifest['images'][name]['status'] = 'approved'
                for key in ('claimed_by', 'claimed_at', 'expires_at'):
                    manifest['images'][name].pop(key, None)
        for name, record in drafts_clean.items():
            if name not in manifest['images']:
                raise ValueError(f'{name}: a draft is missing its verified image identity.')
            if record.get('image_id') not in (None, manifest['images'][name]['image_id']):
                raise ValueError(f'{name}: draft annotations identify a different image.')
            record['image_id'] = manifest['images'][name]['image_id']
        validate_lock_manifest(manifest)
        updates = {}
        if master_clean != master:
            updates['universal_annotations.json'] = json.dumps(master_clean, allow_nan=False).encode()
        if drafts_clean != drafts:
            updates['annotation_drafts.json'] = json.dumps(drafts_clean, allow_nan=False).encode()
        if original != manifest or updates:
            prefix = f'identity_migrations/{revision}'
            for filename, data in (('lock_manifest.json', original), ('universal_annotations.json', master),
                                   ('annotation_drafts.json', drafts)):
                updates[f'{prefix}/{filename}'] = json.dumps(data, allow_nan=False).encode()
        return manifest, updates

    def _read_records(self, filename, revision, manifest):
        from tempo_nuclei import migrate_record
        records = merge_image_records({name:migrate_record(record) for name,record in self._read(filename, revision, {}).items()}, manifest)
        for name, record in records.items():
            identity = manifest.get('images', {}).get(name, {}).get('image_id')
            if identity:
                if record.get('image_id') not in (None, identity):
                    raise ValueError(f'{name}: annotations identify a different image.')
                record['image_id'] = identity
        return records

    def is_maintainer(self, *, raise_on_error=False):
        if not self.api or not self.token:
            return False
        try:
            who = self.api.whoami(token=self.token or False)
            access = who.get('auth', {}).get('accessToken', {})
            scope = access.get('role')
            owner = self.get_dataset_repo().split('/')[0]
            admin = who.get('name') == owner or any(o.get('name') == owner and o.get('roleInOrg') == 'admin' for o in who.get('orgs', []))
            writable = scope == 'write'
            if scope in ('fineGrained', 'fine-grained'):
                writable = any(
                    entry.get('entity', {}).get('name') in (owner, self.get_dataset_repo())
                    and entry.get('entity', {}).get('type') in ('org', 'user', 'dataset')
                    and bool({'repo.write', 'repo.content.write'} & set(entry.get('permissions', [])))
                    for entry in access.get('fineGrained', {}).get('scoped', [])
                )
            elif admin and who.get('auth', {}).get('type') in ('oauth', 'app_token_as_user'):
                # OAuth credentials may have no personal-access-token role.
                # Ask the Hub to verify actual write access for this account.
                self.api.auth_check(repo_id=self.get_dataset_repo(), repo_type='dataset',
                                    token=self.token, write=True)
                writable = True
            return writable and admin
        except Exception:
            if raise_on_error:
                raise
            return False

    def _online(self):
        if hasattr(self, '_credential_lock'):
            self.ensure_credentials()
        if not self.api or not self.token:
            raise RuntimeError('An authenticated online connection is required.')

    def _head(self, repo_type='dataset'):
        self._online()
        if repo_type not in ('dataset', 'model'):
            raise ValueError('Unsupported repository type.')
        # Full dataset metadata is unnecessary for a pinned collaboration read.
        info = self.api.repo_info(repo_id=self.get_dataset_repo() if repo_type == 'dataset' else self.get_models_repo(), repo_type=repo_type,
                                  expand=['sha', 'private'], token=self.token or False)
        if not info.private:
            raise RuntimeError('The shared Hugging Face repository must be private.')
        if not isinstance(info.sha, str) or not info.sha.strip():
            raise ValueError('The dataset revision could not be verified. Retry the request.')
        return info.sha

    def _read(self, filename, revision, default=None, repo_type='dataset'):
        self._online()
        try:
            path = hf_hub_download(repo_id=self.get_dataset_repo() if repo_type == 'dataset' else self.get_models_repo(), filename=filename, revision=revision, repo_type=repo_type, token=self.token or False)
            with open(path, encoding='utf-8') as stream:
                return json.load(stream)
        except EntryNotFoundError:
            if default is not None:
                return copy.deepcopy(default)
            raise

    def fetch_lock_manifest(self, force_refresh=False):
        try:
            self.authenticated_account()
            revision = self._head()
            manifest, _ = self._prepare_manifest(validate_lock_manifest(self._read('lock_manifest.json', revision,
                                                {'version':1,'images':{}})), revision)
            try:
                atomic_json(LOCAL_LOCK_CACHE, manifest)
            except OSError:
                pass  # Cache failure must not discard an authoritative remote read.
            return manifest
        except Exception:
            if force_refresh:
                raise
            try:
                with open(LOCAL_LOCK_CACHE, encoding='utf-8') as stream:
                    return validate_lock_manifest(json.load(stream))
            except (OSError, ValueError):
                return {'version':1,'images':{}}

    def cached_lock_manifest(self):
        try:
            with open(LOCAL_LOCK_CACHE, encoding='utf-8') as stream:
                return validate_lock_manifest(json.load(stream))
        except (OSError, ValueError):
            return {'version':1,'images':{}}

    def _transaction(self, mutate, message, *, reconcile_images=True, skip_unchanged=False):
        """Commit related files together; re-evaluate business rules after a conflict."""
        context = self.token, self.get_dataset_repo(), self.get_models_repo()
        for attempt in range(4):
            if context != (self.token, self.get_dataset_repo(), self.get_models_repo()):
                raise PermissionError('The account or shared repositories changed. Refresh before retrying.')
            revision = self._head()
            manifest = validate_lock_manifest(self._read('lock_manifest.json', revision, {'version':1,'images':{}}))
            original = copy.deepcopy(manifest) if skip_unchanged else None
            identity_updates = {}
            if reconcile_images:
                manifest, identity_updates = self._prepare_manifest(manifest, revision)
            files, result = mutate(manifest, revision)
            actor = self.get_collaborator_id()
            if actor.startswith('hf:'):
                profiles = manifest.setdefault('collaborators', {})
                if not isinstance(profiles, dict):
                    raise ValueError('The shared collaborator directory is malformed.')
                profiles[actor] = dict(profile_name=self.profile_name(),
                                      account_name=self.get_account_display_name())
            files = dict(identity_updates, **files)
            if skip_unchanged and not files and manifest == original:
                if context != (self.token, self.get_dataset_repo(), self.get_models_repo()):
                    raise PermissionError('The account or shared repositories changed. Refresh before retrying.')
                return result
            files['lock_manifest.json'] = json.dumps(manifest, allow_nan=False).encode()
            operations = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=value) for name,value in files.items()]
            if context != (self.token, self.get_dataset_repo(), self.get_models_repo()):
                raise PermissionError('The account or shared repositories changed before the write could complete.')
            try:
                commit_and_confirm(self.api, repo_id=self.get_dataset_repo(), repo_type='dataset',
                                   token=self.token, parent_commit=revision, operations=operations,
                                   commit_message=message, read_json=self._read)
            except Exception as exc:
                if getattr(getattr(exc, 'response', None), 'status_code', None) in (409, 412) and attempt < 3:
                    continue
                raise
            try:
                atomic_json(LOCAL_LOCK_CACHE, manifest)
            except OSError:
                pass  # The remote commit already succeeded.
            return result
        raise RuntimeError('Repository changed repeatedly; retry the operation.')

    @staticmethod
    def _lease(user):
        now = datetime.now(timezone.utc)
        return dict(status='locked', claimed_by=user, claimed_at=now.isoformat(), expires_at=(now+timedelta(days=7)).isoformat())

    @staticmethod
    def _owned(record, user):
        return record.get('status') in ('locked', 'submitted') and record.get('claimed_by') == user and not expired(record)

    def claim_batch(self, image_filenames, collaborator_id, ttl_days=7):
        collaborator_id = self._require_actor(collaborator_id)
        def mutate(manifest, revision):
            claimed, conflicts = [], []
            for name in dict.fromkeys(canonical_image_name(manifest, name) for name in image_filenames):
                rec = manifest['images'].get(name)
                if rec and (rec.get('status') == 'available' or rec.get('status') in ('locked', 'submitted') and (expired(rec) or rec.get('claimed_by') == collaborator_id)):
                    manifest['images'][name] = dict(rec, **self._lease(collaborator_id))
                    if rec.get('status') == 'submitted':
                        manifest['images'][name]['status'] = 'submitted'
                    claimed.append(name)
                else:
                    conflicts.append(name)
            return {}, (claimed, conflicts)
        return self._transaction(mutate, f'Claim batch for {collaborator_id}')

    def renew_claim(self, image_filenames, collaborator_id, ttl_days=7):
        collaborator_id = self._require_actor(collaborator_id)
        def mutate(manifest, revision):
            for name in dict.fromkeys(canonical_image_name(manifest, name) for name in image_filenames):
                rec = manifest['images'].get(name, {})
                if rec.get('status') not in ('locked', 'submitted') or rec.get('claimed_by') != collaborator_id:
                    raise ValueError('Lease was adopted or is no longer editable.')
                manifest['images'][name] = dict(rec, **self._lease(collaborator_id))
                manifest['images'][name]['status'] = rec['status']
            return {}, True
        return self._transaction(mutate, 'Renew seven-day lease')

    def release_claim(self, image_filenames, collaborator_id):
        collaborator_id = self._require_actor(collaborator_id)
        maintainer = self.is_maintainer()
        def mutate(manifest, revision):
            for name in dict.fromkeys(canonical_image_name(manifest, name) for name in image_filenames):
                rec = manifest['images'].get(name, {})
                if rec.get('status') != 'locked' or not (rec.get('claimed_by') == collaborator_id or maintainer):
                    raise ValueError('Only editable leases may be released.')
                manifest['images'][name] = {key:value for key,value in rec.items()
                                          if key not in ('claimed_by','claimed_at','expires_at','submission_id')}
                manifest['images'][name]['status'] = 'available'
            return {}, True
        return self._transaction(mutate, 'Release editable batch')

    def check_orphan_status(self, image_filename, collaborator_id):
        manifest = self.fetch_lock_manifest()
        rec = manifest.get('images', {}).get(canonical_image_name(manifest, image_filename), {})
        status = rec.get('status', 'available')
        if status in ('locked', 'submitted'):
            if rec.get('claimed_by') != collaborator_id:
                return 'adopted_by_other'
            return 'expired_unclaimed' if expired(rec) else 'active'
        return status

    def download_image(self, filename, revision=None, manifest=None):
        image_filename(filename)
        self._online()
        if manifest is not None:
            filename = canonical_image_name(manifest, filename)
        identity = (manifest or {}).get('images', {}).get(filename, {})
        expected = identity.get('sha256')
        target = os.path.join(UNIVERSAL_DIR, filename)
        if expected and os.path.isfile(target):
            if self.calculate_sha256(target) != expected:
                raise FileExistsError(f'{filename}: local image differs from its verified shared identity; it was preserved.')
            return target
        clean_name = re.sub(r'^\d{8}_\d{6}_', '', filename)
        source = None
        candidates = (identity['remote_filename'],) if identity.get('remote_filename') else (filename, clean_name)
        for candidate in dict.fromkeys(candidates):
            try:
                source = hf_hub_download(repo_id=self.get_dataset_repo(), repo_type='dataset', filename=f'images/{candidate}', revision=revision, token=self.token or False)
                break
            except EntryNotFoundError:
                continue
        if source is None:
            raise EntryNotFoundError(f'Image {filename} not found in {self.get_dataset_repo()}')
        os.makedirs(UNIVERSAL_DIR, exist_ok=True)
        digest = self.calculate_sha256(source)
        if expected and digest != expected:
            raise ValueError(f'{filename}: served image checksum changed; no local image was replaced.')
        _install_image_copy(source, target, digest)
        return target

    def _preview_records(self, manifest, revision):
        """Use the same master → draft → submission precedence as checkout."""
        records = self._read_records('universal_annotations.json', revision, manifest)
        records.update(self._read_records('annotation_drafts.json', revision, manifest))
        submissions = {}
        for name, lease in manifest.get('images', {}).items():
            sid = lease.get('submission_id')
            if sid:
                if sid not in submissions:
                    submissions[sid] = self._read_records(f'submissions/{sid}/annotations.json', revision, manifest)
                if name in submissions[sid]:
                    records[name] = submissions[sid][name]
        return records

    def fetch_image_catalog(self):
        """Read a pinned browser snapshot without reserving or changing local drafts."""
        self.authenticated_account()
        revision = self._head()
        manifest, _ = self._prepare_manifest(validate_lock_manifest(self._read('lock_manifest.json', revision)), revision)
        records = self._preview_records(manifest, revision)
        from tempo_nuclei import attach_supplement
        nuclear = self._read('nuclear_annotations.json',revision,{})
        records = {name:attach_supplement(record,nuclear.get(record.get('image_id')))
                   if record.get('status') == 'approved' else record
                   for name,record in records.items()}
        return {'revision': revision, 'manifest': manifest, 'records': records,
                'dataset_repo': self.get_dataset_repo()}

    def fetch_image_preview(self, name, catalog=None):
        """Return immutable Hub-cache input; never install it into an editable session."""
        catalog = catalog or self.fetch_image_catalog()
        if catalog['dataset_repo'] != self.get_dataset_repo():
            raise ValueError('The shared dataset changed. Refresh the image browser.')
        manifest, revision = catalog['manifest'], catalog['revision']
        name = canonical_image_name(manifest, name)
        identity = manifest['images'][name]
        image_filename(name)
        remote = identity.get('remote_filename')
        candidates = (remote,) if remote else (name, re.sub(r'^\d{8}_\d{6}_', '', name))
        path = None
        for candidate in dict.fromkeys(candidates):
            try:
                path = hf_hub_download(repo_id=catalog['dataset_repo'], repo_type='dataset',
                    filename=f'images/{candidate}', revision=revision, token=self.token or False)
                break
            except EntryNotFoundError:
                continue
        if path is None:
            raise EntryNotFoundError(f'Image {name} was not found.')
        if identity.get('sha256') and self.calculate_sha256(path) != identity['sha256']:
            raise ValueError(f'{name}: preview does not match the saved image identity.')
        return {'name': name, 'path': path,
                'record': copy.deepcopy(catalog['records'].get(name, {'boxes': []})),
                'reservation': copy.deepcopy(identity), 'revision': revision}

    def checkout_batch(self, names, user):
        # A damaged or unreadable saved draft is not an empty session. Validate
        # it before making a remote claim or downloading replacement input.
        initial = _read_checkout_session()
        if initial.get('dataset_repo') not in (None, self.get_dataset_repo()):
            raise ValueError('Saved annotations belong to a different shared dataset; they were preserved.')
        user = self._require_actor(user)
        claimed, conflicts = self.claim_batch(names, user)
        revision = self._head()
        manifest, _ = self._prepare_manifest(validate_lock_manifest(self._read('lock_manifest.json', revision)), revision)
        drafts = self._read_records('annotation_drafts.json', revision, manifest)
        master = self._read_records('universal_annotations.json', revision, manifest)
        submissions = {}
        for name in claimed:
            sid = manifest['images'].get(name, {}).get('submission_id')
            if sid and sid not in submissions:
                submissions[sid] = self._read_records(f'submissions/{sid}/annotations.json', revision, manifest)
        # A failed download leaves a resumable lease; never report a cached batch prematurely.
        for name in claimed:
            if manifest.get('version', 1) >= 2:
                self.download_image(name, revision=revision, manifest=manifest)
            else:
                self.download_image(name, revision=revision)
        latest_revision = self._head()
        if latest_revision != revision:
            latest = validate_lock_manifest(self._read('lock_manifest.json', latest_revision))
            if any(latest['images'].get(name) != manifest['images'].get(name) for name in claimed):
                raise ValueError('A reservation changed during checkout. Local annotations were preserved; retry.')
        self._require_actor(user)
        # Pick up completed local saves while remote reads/downloads were waiting.
        def finish_session(session):
            if session.get('dataset_repo') not in (None, self.get_dataset_repo()):
                raise ValueError('Saved annotations belong to a different shared dataset; they were preserved.')
            session['collaborator_id'] = user
            session['claimed_images'] = sorted(set(session.get('claimed_images', [])) | set(claimed))
            annotations = merge_image_records(session.get('annotations', {}), manifest)
            session['annotations'] = annotations
            session['claimed_images'] = sorted(set(canonical_image_name(manifest, name)
                                                   for name in session['claimed_images']))
            for name in claimed:
                lease = manifest['images'].get(name, {})
                if not self._owned(lease, user):
                    raise ValueError(f'{name}: lease changed during checkout; retry.')
                record = drafts.get(name, master.get(name))
                if lease.get('submission_id'):
                    submitted = submissions[lease['submission_id']]
                    record = submitted.get(name, record)
                local = annotations.get(name)
                if local:
                    if local.get('sync_pending') and local.get('draft_owner') == user:
                        record = local  # Do not replace newer unsynced edits with the cloud snapshot.
                    elif local.get('draft_owner') not in (None, user) and record is None:
                        record = {'boxes':[]}  # A new owner cannot silently inherit a private unsynced draft.
                if record is not None:
                    if name in annotations and annotations[name] != record:
                        backup = os.path.join(UNIVERSAL_DIR, f'checkout_backup_{uuid.uuid4().hex}.json')
                        atomic_json(backup, {name:annotations[name]})
                    annotations[name] = copy.deepcopy(record)
                else:
                    annotations.setdefault(name, {'boxes':[]})
                annotations[name]['status'] = lease['status']
                annotations[name]['draft_owner'] = user
                if lease.get('image_id'):
                    annotations[name]['image_id'] = lease['image_id']
            session["dataset_repo"] = self.get_dataset_repo()
            return session
        update_session(LOCAL_SESSION_PATH, finish_session)
        return claimed, conflicts

    def upload_raw_images(self, local_image_paths, progress_callback=None):
        try:
            if not self.is_maintainer():
                raise PermissionError('Only the maintainer can upload raw images.')
            def mutate(manifest, revision):
                files = {}
                digests = {record.get('sha256') for record in manifest['images'].values()}
                for path in local_image_paths:
                    name = image_filename(os.path.basename(path))
                    digest = self.calculate_sha256(path)
                    if digest in digests:
                        raise ValueError(f'{name}: these image bytes are already in the pool. Use the existing image.')
                    if name in manifest['images'] or name in manifest.get('aliases', {}) or f'images/{name}' in files:
                        raise ValueError(f'{name} already exists; images cannot be overwritten.')
                    digests.add(digest)
                    files[f'images/{name}'] = path
                    manifest['images'][name] = dict(status='available', image_id='sha256:'+digest,
                                                   sha256=digest, remote_filename=name, original_filename=name)
                return files, len(files)
            count = self._transaction(mutate, 'Ingest raw image pool')
            return count, f'Uploaded {count} images.'
        except Exception as exc:
            return 0, redact_credentials(exc, self.token)

    def upload_submission(self, collaborator_id, image_filenames, annotations_dict, metadata_dict, progress_callback=None):
        try:
            collaborator_id = self._require_actor(collaborator_id)
            validate_annotations(annotations_dict)
            if set(image_filenames) != set(annotations_dict):
                raise ValueError('Submission image list does not match annotations.')
            sid = uuid.uuid4().hex
            def mutate(manifest, revision):
                records = merge_image_records(annotations_dict, manifest)
                from tempo_nuclei import is_nucleus
                if any(is_nucleus(shape) for record in records.values() for shape in record.get('boxes',[])):
                    manifest['version']=3
                names = list(dict.fromkeys(canonical_image_name(manifest, name) for name in image_filenames))
                if len(names) != len(image_filenames):
                    raise ValueError('The selection contains duplicate names for the same image.')
                drafts = self._read_records('annotation_drafts.json', revision, manifest)
                for name in names:
                    rec = manifest['images'].get(name, {})
                    if not self._owned(rec, collaborator_id) or rec.get('status') != 'locked':
                        raise ValueError(f'{name}: current seven-day lease is required.')
                    if records[name].get('image_id') not in (None, rec.get('image_id')):
                        raise ValueError(f'{name}: annotation image identity changed.')
                    if rec.get('image_id'):
                        records[name]['image_id'] = rec['image_id']
                    records[name]['status'] = 'submitted'
                meta = dict(submission_id=sid, collaborator_id=collaborator_id, profile_name=self.profile_name(), timestamp=datetime.now(timezone.utc).isoformat(), image_count=len(names), metadata=metadata_dict)
                for name in names:
                    manifest['images'][name] = dict(manifest['images'][name], status='submitted', submission_id=sid)
                    drafts[name] = copy.deepcopy(records[name])
                return {'annotation_drafts.json':json.dumps(drafts).encode(), f'submissions/{sid}/annotations.json':json.dumps(records).encode(), f'submissions/{sid}/metadata.json':json.dumps(meta).encode()}, sid
            return True, self._transaction(mutate, f'Submit annotation batch {sid}')
        except Exception as exc:
            return False, redact_credentials(exc, self.token)

    def sync_annotation_drafts(self, collaborator_id, annotations):
        """Save the adoptable state and any pending submission in one transaction."""
        validate_annotations(annotations, allow_unclassified=True)
        collaborator_id = self._require_actor(collaborator_id)
        def mutate(manifest, revision):
            records = merge_image_records(annotations, manifest)
            from tempo_nuclei import is_nucleus
            if any(is_nucleus(shape) for record in records.values() for shape in record.get('boxes',[])):
                manifest['version']=3
            drafts = self._read_records('annotation_drafts.json', revision, manifest)
            submissions = {}
            for name, record in records.items():
                lease = manifest['images'].get(name, {})
                if not self._owned(lease, collaborator_id):
                    raise ValueError(f'{name}: lease expired, was adopted, or annotations were approved. Local edits are preserved.')
                clean = copy.deepcopy(record)
                if clean.get('image_id') not in (None, lease.get('image_id')):
                    raise ValueError(f'{name}: annotation image identity changed.')
                if lease.get('image_id'):
                    clean['image_id'] = lease['image_id']
                clean['status'] = lease['status']
                drafts[name] = clean
                if lease.get('status') == 'submitted':
                    validate_annotations({name: clean})
                    sid = lease['submission_id']
                    if sid not in submissions:
                        submissions[sid] = self._read_records(f'submissions/{sid}/annotations.json', revision, manifest)
                    if name not in submissions[sid]:
                        raise ValueError('Image is missing from its pending submission.')
                    submissions[sid][name] = clean
            files = {'annotation_drafts.json':json.dumps(drafts).encode()}
            files.update({f'submissions/{sid}/annotations.json':json.dumps(records).encode() for sid,records in submissions.items()})
            return files, len(records)
        return self._transaction(mutate, 'Sync annotation drafts and pending submissions')

    def approve_local_images(self, annotations, image_paths, collaborator_id):
        """Maintainer approval of an explicitly selected, complete local batch."""
        if not self.is_maintainer():
            raise PermissionError('Maintainer account required.')
        collaborator_id = self._require_actor(collaborator_id)
        validate_annotations(annotations)
        if set(annotations) != set(image_paths):
            raise ValueError('Selected images and annotations must match.')
        for name, record in annotations.items():
            if not any(b.get('class_id') in (0, 1) for b in record['boxes']):
                raise ValueError(f'{name}: no cell annotations to approve.')
        def mutate(manifest, revision):
            master = self._read_records('universal_annotations.json', revision, manifest)
            records = merge_image_records(annotations, manifest)
            from tempo_nuclei import is_nucleus, supplement, nuclear_labels
            nuclear = self._read('nuclear_annotations.json',revision,{})
            paths = {canonical_image_name(manifest, name):path for name,path in image_paths.items()}
            if len(paths) != len(image_paths):
                raise ValueError('Selected aliases identify the same image more than once.')
            files = {}
            existing = set(self.api.list_repo_files(repo_id=self.get_dataset_repo(), repo_type='dataset', revision=revision, token=self.token or False))
            digests = {entry.get('sha256'):name for name,entry in manifest['images'].items() if entry.get('sha256')}
            for name, record in records.items():
                lease = manifest['images'].get(name, {})
                if lease.get('status') == 'submitted' or (lease.get('status') == 'locked' and not self._owned(lease, collaborator_id)):
                    raise ValueError(f'{name}: review its submission or wait for its current lease.')
                if lease.get('status') == 'approved' or master.get(name, {}).get('status') == 'approved':
                    raise ValueError(f'{name}: already approved; existing approvals are preserved.')
                digest = self.calculate_sha256(paths[name])
                known = digests.get(digest)
                if known is not None and known != name:
                    raise ValueError(f'{name}: the same image already exists as {known}. Select that image instead.')
                digests[digest] = name
                remote_filename = lease.get('remote_filename', name)
                remote_name = f'images/{remote_filename}'
                if lease.get('sha256') and lease['sha256'] != digest:
                    raise ValueError(f'{name}: local image differs from its verified image identity.')
                if remote_name in existing:
                    cached = hf_hub_download(repo_id=self.get_dataset_repo(), filename=remote_name,
                                             repo_type='dataset', revision=revision, token=self.token or False)
                    if self.calculate_sha256(cached) != digest:
                        raise ValueError(f'{name}: local image differs from the remote image.')
                else:
                    files[remote_name] = str(paths[name])
                approved = dict(copy.deepcopy(record),status='approved',image_id='sha256:'+digest)
                if any(is_nucleus(shape) for shape in approved['boxes']):
                    manifest['version']=3
                    nuclear_labels(dict(approved,nuclear_status='approved'),(approved['height'],approved['width']))
                    nuclear[approved['image_id']]=supplement(approved,status='approved')
                master[name] = dict(approved,boxes=[shape for shape in approved['boxes'] if not is_nucleus(shape)])
                manifest['images'][name] = dict(lease, status='approved', approved_by=collaborator_id,
                                              image_id='sha256:'+digest, sha256=digest, remote_filename=remote_filename)
                for key in ('claimed_by', 'claimed_at', 'expires_at'):
                    manifest['images'][name].pop(key, None)
            files['universal_annotations.json'] = json.dumps(master).encode()
            files['nuclear_annotations.json'] = json.dumps(nuclear).encode()
            return files, len(records)
        return self._transaction(mutate, 'Approve selected complete local images')

    def reserve_nuclear_annotations(self, name):
        """Independent lease: approved cell outlines remain immutable."""
        actor = self._require_actor()
        def mutate(manifest, revision):
            from tempo_nuclei import attach_supplement
            master = self._read_records('universal_annotations.json',revision,manifest)
            canonical = canonical_image_name(manifest,name)
            record = master.get(canonical)
            if not record or record.get('status') != 'approved':
                raise ValueError('Select an approved image for nuclear annotation.')
            manifest['version'] = 3
            leases = manifest.setdefault('nuclear_leases',{})
            lease = leases.get(canonical,{})
            if lease.get('status') == 'submitted':
                raise ValueError('Nuclear annotations are awaiting review.')
            if lease and not expired(lease) and lease.get('claimed_by') != actor:
                raise ValueError('Nuclear annotations are reserved by another collaborator.')
            leases[canonical] = dict(self._lease(actor),status='locked')
            drafts = self._read('nuclear_drafts.json',revision,{})
            nuclear = drafts.get(record['image_id']) or self._read('nuclear_annotations.json',revision,{}).get(record['image_id'])
            result = attach_supplement(record,nuclear)
            result['nuclear_only'] = True
            result['nuclear_expected'] = drafts.get(record['image_id'])
            return {}, result
        return self._transaction(mutate,'Reserve nuclear annotation layer')

    def renew_nuclear_annotations(self, name, *, expected=None):
        actor=self._require_actor()
        def mutate(manifest,revision):
            canonical=canonical_image_name(manifest,name)
            lease=manifest.get('nuclear_leases',{}).get(canonical,{})
            if lease.get('claimed_by')!=actor or lease.get('status')!='locked':
                raise ValueError('The nuclear reservation is no longer yours; reload its current state.')
            identity=manifest['images'][canonical]['image_id']
            if self._read('nuclear_drafts.json',revision,{}).get(identity)!=expected:
                raise ValueError('Nuclear draft changed; reload before renewing.')
            manifest['nuclear_leases'][canonical]=dict(self._lease(actor),status='locked')
            return {},True
        return self._transaction(mutate,'Renew nuclear annotation reservation')

    def save_nuclear_annotations(self, name, record, *, submit=False, expected=None):
        actor = self._require_actor()
        validate_annotations({name:record})
        def mutate(manifest, revision):
            from tempo_nuclei import cell_digest, supplement
            canonical = canonical_image_name(manifest,name)
            master = self._read_records('universal_annotations.json',revision,manifest)[canonical]
            if record.get('image_id') != master.get('image_id'):
                raise ValueError('Nuclear annotations identify a different image.')
            if cell_digest(master) != cell_digest(record):
                raise ValueError('Approved cell geometry changed; reload before editing nuclei.')
            lease = manifest.get('nuclear_leases',{}).get(canonical,{})
            if not self._owned(lease,actor) or lease.get('status') != 'locked':
                raise ValueError('Reserve the nuclear layer before saving.')
            drafts = self._read('nuclear_drafts.json',revision,{})
            old = drafts.get(master['image_id'])
            if old != expected:
                raise ValueError('Nuclear draft changed; reload before saving.')
            nuclear = supplement(record,status='submitted' if submit else 'draft')
            nuclear['revision'] = (old or {}).get('revision',0)+1
            drafts[master['image_id']] = nuclear
            files = {'nuclear_drafts.json':json.dumps(drafts).encode()}
            if submit:
                sid = uuid.uuid4().hex
                manifest['nuclear_leases'][canonical] = dict(lease,status='submitted',submission_id=sid)
                files[f'submissions/{sid}/metadata.json'] = json.dumps(dict(submission_id=sid,kind='nuclei',collaborator_id=actor,
                    profile_name=self.profile_name(),image_count=1,timestamp=datetime.now(timezone.utc).isoformat())).encode()
                files[f'submissions/{sid}/annotations.json'] = json.dumps({canonical:dict(record,nuclear_only=True)}).encode()
            return files,nuclear
        return self._transaction(mutate,'Submit nuclear outlines' if submit else 'Save nuclear draft')

    def _review_nuclear(self, submission_id, feedback=None, edited_annotations=None, expected_annotations=None):
        if not self.is_maintainer():
            raise PermissionError('Maintainer account required.')
        reviewer = self._require_actor()
        def mutate(manifest,revision):
            from tempo_nuclei import cell_digest, supplement
            prefix = f'submissions/{submission_id}'
            original = self._read_records(f'{prefix}/annotations.json',revision,manifest)
            if expected_annotations is not None and original != expected_annotations:
                raise ValueError('Nuclear annotations changed during review; reload.')
            records = copy.deepcopy(edited_annotations if edited_annotations is not None else original)
            if set(records) != set(original):
                raise ValueError('Review cannot change the image set.')
            validate_annotations(records)
            master = self._read_records('universal_annotations.json',revision,manifest)
            approved = self._read('nuclear_annotations.json',revision,{})
            drafts = self._read('nuclear_drafts.json',revision,{})
            for name,record in records.items():
                lease = manifest.get('nuclear_leases',{}).get(name,{})
                if lease.get('status') != 'submitted' or lease.get('submission_id') != submission_id:
                    raise ValueError('Nuclear submission was already reviewed.')
                if (record.get('image_id') != master[name].get('image_id')
                        or cell_digest(record) != cell_digest(master[name])):
                    raise ValueError('Nuclear review cannot modify approved cell outlines.')
                if feedback is not None:
                    manifest['nuclear_leases'][name] = dict(self._lease(lease['claimed_by']),status='locked')
                else:
                    from tempo_nuclei import nuclear_labels
                    nuclear_labels(dict(record,nuclear_status='approved'),(record['height'],record['width']))
                    approved[master[name]['image_id']] = supplement(record,status='approved')
                    approved[master[name]['image_id']]['approved_by'] = reviewer
                    drafts.pop(master[name]['image_id'],None)
                    manifest['nuclear_leases'].pop(name,None)
            if feedback is not None:
                return {f'{prefix}/feedback.json':json.dumps({'feedback':feedback}).encode()},True
            return {'nuclear_annotations.json':json.dumps(approved).encode(),
                    'nuclear_drafts.json':json.dumps(drafts).encode(),
                    f'{prefix}/approved.json':json.dumps({'approved_by':reviewer}).encode(),
                    f'{prefix}/annotations.json':json.dumps(records).encode()},True
        return self._transaction(mutate,'Review nuclear supplement')

    def fetch_pending_submissions(self):
        self._online()
        if not self.is_maintainer(raise_on_error=True):
            raise PermissionError('Maintainer access is required to review submissions.')
        revision = self._head()
        files = set(self.api.list_repo_files(repo_id=self.get_dataset_repo(), repo_type='dataset', revision=revision, token=self.token or False))
        profiles = self._read('lock_manifest.json', revision, {}).get('collaborators', {})
        submissions = []
        for name in sorted(files):
            if not name.startswith('submissions/') or not name.endswith('/metadata.json') or any(
                    name.replace('metadata.json', marker) in files for marker in ('feedback.json', 'approved.json')):
                continue
            record = self._read(name, revision)
            profile = profiles.get(record.get('collaborator_id'), {})
            if isinstance(profile, dict) and safe_profile_name(profile.get('profile_name'), self.token):
                record['profile_name'] = profile['profile_name']
            submissions.append(record)
        return submissions

    def fetch_submission_annotations(self, submission_id):
        revision = self._head()
        manifest, _ = self._prepare_manifest(validate_lock_manifest(self._read('lock_manifest.json', revision)), revision)
        return self._read_records(f'submissions/{submission_id}/annotations.json', revision, manifest)

    def _review(self, submission_id, feedback=None, edited_annotations=None, expected_annotations=None):
        if self._read(f'submissions/{submission_id}/metadata.json',self._head(),{}).get('kind') == 'nuclei':
            return self._review_nuclear(submission_id,feedback,edited_annotations,expected_annotations)
        if not self.is_maintainer():
            raise PermissionError('Maintainer account required.')
        reviewer = self._require_actor()
        def mutate(manifest, revision):
            prefix = f'submissions/{submission_id}'
            original = self._read_records(f'{prefix}/annotations.json', revision, manifest)
            if expected_annotations is not None and original != expected_annotations:
                raise ValueError('Annotations changed while you were reviewing. Reload the submission before approving.')
            meta = self._read(f'{prefix}/metadata.json', revision)
            for name in original:
                rec = manifest['images'].get(name, {})
                if rec.get('status') != 'submitted' or rec.get('submission_id') != submission_id:
                    raise ValueError('Submission was already reviewed or its ownership changed.')
            if feedback is not None:
                for name in original:
                    owner = manifest['images'][name]['claimed_by']
                    manifest['images'][name] = dict(manifest['images'][name], **self._lease(owner),
                                                   feedback=feedback, revision_requested=True)
                    manifest['images'][name].pop('submission_id', None)
                files = {f'{prefix}/feedback.json':json.dumps({'feedback':feedback,'collaborator_id':meta['collaborator_id']}).encode()}
            else:
                approved = copy.deepcopy(edited_annotations if edited_annotations is not None else original)
                if set(approved) != set(original):
                    raise ValueError('Review cannot change the submitted image set.')
                validate_annotations(approved)
                for name, record in approved.items():
                    if not any(box.get('class_id') in (0, 1) for box in record.get('boxes', [])):
                        raise ValueError(f'{name}: cannot approve an image without cell annotations. '
                                         'Finish annotating all cells before approval.')
                master = self._read_records('universal_annotations.json', revision, manifest)
                nuclear = self._read('nuclear_annotations.json',revision,{})
                from tempo_nuclei import is_nucleus, supplement
                for name, record in approved.items():
                    if master.get(name, {}).get('status') == 'approved':
                        raise ValueError(f'{name} is already approved; its cell outlines are locked.')
                    record['status'] = 'approved'
                    if manifest['images'][name].get('image_id'):
                        record['image_id'] = manifest['images'][name]['image_id']
                    if any(is_nucleus(shape) for shape in record['boxes']):
                        from tempo_nuclei import nuclear_labels
                        nuclear_labels(dict(record,nuclear_status='approved'),(record['height'],record['width']))
                        nuclear[record['image_id']] = supplement(record,status='approved')
                    master[name] = dict(record,boxes=[shape for shape in record['boxes'] if not is_nucleus(shape)])
                    manifest['images'][name] = dict(manifest['images'][name], status='approved',
                                                   submission_id=submission_id, approved_by=reviewer)
                    for key in ('claimed_by', 'claimed_at', 'expires_at'):
                        manifest['images'][name].pop(key, None)
                files = {'nuclear_annotations.json':json.dumps(nuclear).encode(), 'universal_annotations.json':json.dumps(master).encode(), f'{prefix}/approved.json':json.dumps({'approved_at':datetime.now(timezone.utc).isoformat()}).encode(), f'{prefix}/annotations.json':json.dumps(approved).encode()}
            return files, True
        self._transaction(mutate, f'Review submission {submission_id}')

    def merge_submission_to_master(self, submission_id, edited_annotations=None, expected_annotations=None):
        try:
            self._review(submission_id, edited_annotations=edited_annotations, expected_annotations=expected_annotations)
            return True, 'Annotations approved for training.'
        except Exception as exc:
            return False, redact_credentials(exc, self.token)

    def reject_submission(self, submission_id, feedback_text):
        try:
            if not feedback_text.strip():
                raise ValueError('Feedback is required.')
            self._review(submission_id, feedback=feedback_text)
            return True, 'Batch reopened for its original author with feedback.'
        except Exception as exc:
            return False, redact_credentials(exc, self.token)

    @staticmethod
    def calculate_sha256(filepath):
        digest = hashlib.sha256()
        with open(filepath, 'rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def model_manifest(self):
        self._online()
        info = self.api.repo_info(repo_id=self.get_models_repo(), repo_type='model', token=self.token or False)
        result = self._read('model_manifest.json', info.sha, default={'models':{}}, repo_type='model')
        if not isinstance(result, dict):
            raise ValueError('Invalid published model manifest.')
        result = dict(result)
        result['revision'] = info.sha
        result['models'] = _model_records(result)
        return result

    def download_model_release(self, manifest, target_dir=None, *, local_sources=None, progress=None):
        """Verify a whole release before replacing any installed weights."""
        models = _model_records(manifest)
        if not models:
            return 0
        if target_dir is None:
            from tempo_model_catalog import installed_model_dir
            target_dir = installed_model_dir(self.get_models_repo())
        os.makedirs(target_dir, exist_ok=True)
        from tempo_annotation_store import session_lock
        try:
            with session_lock(Path(target_dir)/'.model-install'):
                return self._install_model_release(manifest, models, target_dir, local_sources, progress)
        except BlockingIOError:
            raise RuntimeError('Another TEMPO instance is installing models. Wait for it to finish, then retry.') from None

    def _install_model_release(self, manifest, models, target_dir, local_sources, progress):
        from tempo_model_transfer import ModelTransferReporter
        reporter = ModelTransferReporter(progress, models)
        folder = tempfile.mkdtemp(dir=target_dir, prefix='.model-update-')
        retain_backups = False
        try:
            staged, backups, attempted = {}, {}, []
            downloads = Path(folder)/'downloads'; downloads.mkdir()
            backup_folder = Path(folder)/'backups'; backup_folder.mkdir()
            for name, record in models.items():
                reporter.emit('checking', name)
                source=(local_sources or {}).get(name)
                if source is None:
                    source = hf_hub_download(repo_id=self.get_models_repo(), filename=name, revision=manifest['revision'], repo_type='model', token=self.token or False,
                                             tqdm_class=reporter.download_class(name))
                reporter.emit('verifying', name)
                path = downloads/name; shutil.copy2(source,path)
                if self.calculate_sha256(path) != record['sha256']:
                    raise ValueError(f'{name}: checksum mismatch. Existing models were preserved.')
                staged[name] = path
                reporter.emit('verified', name)
            from tempo_cellpose import bundle_metadata
            from tempo_model_catalog import MODEL_FILES
            if MODEL_FILES['cellpose'] in staged:
                dependency = bundle_metadata(staged[MODEL_FILES['cellpose']]).get('helper_sha256')
                if dependency and (MODEL_FILES['stardist'] not in models or models[MODEL_FILES['stardist']]['sha256'] != dependency):
                    raise ValueError('The release pairs Cellpose with a different nuclear helper. Existing models were preserved.')
            try:
                for name,path in staged.items():
                    reporter.emit('installing', name)
                    target = Path(target_dir)/name
                    if target.exists():
                        backup = backup_folder/(name+'.previous'); shutil.copy2(target,backup); backups[name]=backup
                    attempted.append(name)
                    os.replace(path,target)
            except BaseException as installation_error:
                retain_backups = True
                rollback_errors = []
                for name in attempted:
                    target=Path(target_dir)/name
                    try:
                        if (name in backups and target.is_file()
                                and self.calculate_sha256(target) == self.calculate_sha256(backups[name])):
                            continue  # This replacement failed without changing the incumbent.
                        if name in backups: os.replace(backups[name],target)
                        elif target.exists(): target.unlink()
                    except BaseException as exc:
                        rollback_errors.append(f'{name}: {type(exc).__name__}: {exc}')
                if rollback_errors:
                    raise RuntimeError(f'Model installation failed; rollback could not finish. Model backups are retained in {folder}. '
                                       + ' '.join(rollback_errors)) from installation_error
                retain_backups = False
                raise
        except BaseException:
            reporter.emit('failed')
            raise
        finally:
            if not retain_backups:
                shutil.rmtree(folder)
        reporter.emit('complete')
        return len(models)

    def download_champion_weights(self, model_filename, target_dir=None, expected_sha256=None, progress_callback=None):
        tmp = None
        try:
            from tempo_model_catalog import INSTALLABLE_MODEL_FILES, installed_model_dir
            if model_filename not in INSTALLABLE_MODEL_FILES:
                raise ValueError('Unknown TEMPO model filename.')
            if target_dir is None:
                target_dir = installed_model_dir(self.get_models_repo())
            manifest = self.model_manifest()
            record = manifest['models'][model_filename]
            expected = record['sha256']
            if expected_sha256 and expected_sha256.lower() != expected.lower():
                raise ValueError('Model release changed; refresh before downloading.')
            target = os.path.join(target_dir, model_filename)
            if os.path.exists(target) and self.calculate_sha256(target).lower() == expected.lower():
                return True, target
            source = hf_hub_download(repo_id=self.get_models_repo(), filename=model_filename, revision=manifest['revision'], repo_type='model', token=self.token or False)
            os.makedirs(target_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=target_dir, suffix='.tmp')
            os.close(fd)
            shutil.copy2(source, tmp)
            if self.calculate_sha256(tmp).lower() != expected.lower():
                raise ValueError('Downloaded model checksum does not match its release manifest.')
            os.replace(tmp, target)
            return True, target
        except Exception as exc:
            return False, redact_credentials(exc, self.token)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)


tempo_hub = TempoHub()
