"""Standard-library bootstrap used before Colab's isolated packages are installed."""
import json
import hashlib
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class HubRedirectHandler(HTTPRedirectHandler):
    """Allow HTTPS storage redirects without forwarding Hub credentials."""
    def redirect_request(self, request, fp, code, message, headers, newurl):
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        if new.scheme != 'https' or new.username or new.password:
            raise ValueError('An authenticated download attempted an unsafe redirect.')
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None and (old.scheme, old.hostname, old.port or 443) != (new.scheme, new.hostname, new.port or 443):
            for name in ('Authorization', 'Cookie', 'Proxy-authorization'):
                redirected.remove_header(name)
        return redirected


def urlopen(request, *, timeout):
    """Use platform trust plus bundled roots when the desktop runtime needs them."""
    import ssl
    context = ssl.create_default_context()
    try:
        import certifi
    except ImportError:  # The isolated notebook bootstrap uses system roots.
        pass
    else:
        context.load_verify_locations(cafile=certifi.where())
    parsed = urlsplit(request.full_url)
    if parsed.scheme != 'https' or parsed.hostname != 'huggingface.co' or parsed.username or parsed.password:
        raise ValueError('Authenticated downloads must start at Hugging Face over HTTPS.')
    return build_opener(HubRedirectHandler(), HTTPSHandler(context=context)).open(request, timeout=timeout)


def _read_request(url, token):
    request=Request(url,headers={'Authorization':'Bearer '+token})
    try:
        with urlopen(request,timeout=30) as response:
            data=response.read(128*1024+1)
    except HTTPError as error:
        if error.code==404:
            error.close()
            return None
        error.close()
        raise RuntimeError(f'Cannot read the prepared Model Evaluation (HTTP {error.code}). Check notebook token access.') from None
    if len(data)>128*1024:
        raise ValueError('The prepared evaluation is too large.')
    job=json.loads(data)
    if not isinstance(job, dict):
        raise ValueError('The prepared Model Evaluation settings are invalid.')
    return job


def prepared_evaluation(dataset_repo, models_repo, token):
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) for repo in (dataset_repo, models_repo)):
        raise ValueError('Repository names must use owner/name.')
    # Only model-repository writers may authorize executable evaluation content.
    job=_read_request(f'https://huggingface.co/{models_repo}/resolve/main/training/evaluation_request.json', token)
    if job is None:
        # Detect an older selection without accepting any of its code or hashes.
        # Never turn an intended evaluation into paid regular training silently.
        legacy=_read_request(f'https://huggingface.co/datasets/{dataset_repo}/resolve/main/training/evaluation_request.json', token)
        if legacy:
            raise RuntimeError('This evaluation was queued by an older TEMPO version. Recreate it from reviewed protocol sources using the updated application and notebook. Existing files have been retained.')
        return None
    if not job:
        return None
    if (job.get('trust')!='protected-model-repository-v1' or job.get('mode')!='model-evaluation' or job.get('dataset_repo')!=dataset_repo or
            job.get('models_repo')!=models_repo or not re.fullmatch('[a-f0-9]{32}',job.get('id','')) or
            not re.fullmatch('[a-f0-9]{32}',job.get('evaluation_id','')) or
            job.get('gpu') not in ('A100','Any CUDA GPU') or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}',job.get('phase','')) or
            not re.fullmatch(r'[a-f0-9]{64}',job.get('protocol_sha256','')) or
            not re.fullmatch(r'[a-f0-9]{64}',job.get('bundle_sha256','')) or
            not re.fullmatch(r'[a-f0-9]{64}',job.get('plan_sha256',''))):
        raise ValueError('The prepared Model Evaluation settings are invalid.')
    if job.get('status')!='queued':
        raise RuntimeError('This Model Evaluation is '+str(job.get('status'))+'. Review it in TEMPO, queue its next stage or return to regular training before choosing Run all.')
    return job


def evaluation_bundle(job, token):
    url=(f"https://huggingface.co/{job['models_repo']}/resolve/main/"
         f"training/evaluations/{job['evaluation_id']}/bundle.zip")
    with urlopen(Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as response:
        data=response.read(20*1024*1024+1)
    if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=job['bundle_sha256']:
        raise ValueError('The frozen Model Evaluation source bundle failed verification.')
    return data


def protocol_requirements(job,token):
    """Read optional dependencies from the frozen study package without executing it."""
    import io
    import zipfile
    from pathlib import PurePosixPath
    url=(f"https://huggingface.co/{job['models_repo']}/resolve/main/"
         f"training/evaluations/{job['evaluation_id']}/protocol.zip")
    with urlopen(Request(url,headers={'Authorization':'Bearer '+token}),timeout=60) as response:
        data=response.read(20*1024*1024+1)
    if len(data)>20*1024*1024 or hashlib.sha256(data).hexdigest()!=job['protocol_sha256']:
        raise ValueError('The frozen protocol archive failed verification.')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or sum(row.file_size for row in archive.infolist())>20*1024*1024:
            raise ValueError('Invalid protocol archive.')
        descriptor=json.loads(archive.read('protocol.json'));name=descriptor.get('requirements')
        if name is None:return None
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or name not in descriptor.get('files',[]):
            raise ValueError('Invalid protocol dependency path.')
        return archive.read(name)
