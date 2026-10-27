"""Select one checksummed C++ runtime for every standard DLL in the bundle."""
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path, PureWindowsPath
import sys
import shutil


def collect_wheel_libraries(prefix=None, distributions=None, manifest=None):
    """Keep each wheel's private DLLs in its own .libs directory."""
    import base64
    prefix = Path(prefix or sys.prefix).resolve()
    datas, binaries, entries = [], [], []
    for distribution in (distributions if distributions is not None else importlib.metadata.distributions()):
        for record in distribution.files or ():
            relative = Path(str(record).replace('\\','/'))
            if not any(part.endswith('.libs') for part in relative.parts[:-1]): continue
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError(f'Unsafe wheel library destination: {record}')
            source = Path(distribution.locate_file(record)).resolve()
            if not source.is_relative_to(prefix):
                raise RuntimeError(f'Wheel library source escapes the build venv: {source}')
            digest = hashlib.sha256(source.read_bytes()).digest()
            expected = (manifest or {}).get('files',{}).get(source.name.lower(),{}).get('sha256')
            if (digest.hex() != expected and (not record.hash or record.hash.mode != 'sha256'
                    or record.hash.value != base64.urlsafe_b64encode(digest).decode().rstrip('='))):
                raise RuntimeError(f'Wheel library checksum mismatch: {source}')
            binary = source.suffix.lower() in ('.dll','.pyd')
            (binaries if binary else datas).append((str(source),relative.parent.as_posix()))
            entries.append({'provider':distribution.metadata['Name'],'source':str(source),
                            'destination':relative.as_posix(),'sha256':digest.hex(),'binary':binary})
    return datas, binaries, {'success':True,'files':entries}


def canonicalize_vc_runtime(binaries, manifest, prefix=None, verify_signature=None):
    prefix = Path(prefix or sys.prefix)
    selected = {}
    signatures = {}
    for name, identity in manifest['files'].items():
        candidates = (prefix / 'Scripts' / name, prefix / name)
        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise RuntimeError(f'Missing pinned C++ runtime: {name}')
        if hashlib.sha256(source.read_bytes()).hexdigest() != identity['sha256']:
            raise RuntimeError(f'Pinned C++ runtime checksum mismatch: {source}')
        if verify_signature is not None:
            signature = verify_signature(source)
            if (signature.get('status') != 'Valid'
                    or not re.search(r'(?:^|,\s*)O=Microsoft Corporation(?:,|$)', signature.get('subject', ''))):
                raise RuntimeError(f'Pinned C++ runtime has no valid Microsoft signature: {source}')
            signatures[name] = signature
        selected[name] = source
    result, replacements = [], []
    for destination, source, kind in binaries:
        name = PureWindowsPath(destination).name.lower()
        if name in selected:
            replacements.append({'destination': destination, 'previous_source': source,
                                 'selected_source': str(selected[name])})
            source = str(selected[name])
        result.append((destination, source, kind))
    destinations = {destination.lower() for destination, _, _ in result}
    for name, source in selected.items():
        if name not in destinations:
            result.append((name, str(source), 'BINARY'))
    return result, {'package': manifest['package'], 'version': manifest['version'],
                    'files': manifest['files'], 'signatures': signatures, 'replacements': replacements}


def assemble_windows_cpp_runtime(binaries, root):
    """Replace native copies and retain the audit for the real packaging run."""
    from tempo_release import windows_signature
    manifest = json.loads((root / 'installer/windows-native-runtime.json').read_text())
    normalized, audit = canonicalize_vc_runtime(binaries, manifest, verify_signature=windows_signature)
    (root / 'Output/native-runtime-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    return normalized


def prepare_build_environment(manifest, output, prefix=None, distributions=None, verify_signature=None):
    """Use the pinned runtime during analysis too, within the build venv only."""
    import base64
    prefix = Path(prefix or sys.prefix).resolve()
    _, audit = canonicalize_vc_runtime([], manifest, prefix, verify_signature)
    output = Path(output)
    report = {'success':False,'version':manifest['version'],'signatures':audit['signatures'],'overrides':[]}
    try:
        for distribution in (distributions if distributions is not None else importlib.metadata.distributions()):
            for record in distribution.files or ():
                name = PureWindowsPath(str(record)).name.lower()
                if name not in manifest['files']: continue
                destination = Path(distribution.locate_file(record)).resolve()
                if not destination.is_relative_to(prefix):
                    raise RuntimeError(f'C++ override would escape the build venv: {destination}')
                current = hashlib.sha256(destination.read_bytes()).digest()
                if current.hex() == manifest['files'][name]['sha256']: continue
                if (not record.hash or record.hash.mode != 'sha256'
                        or record.hash.value != base64.urlsafe_b64encode(current).decode().rstrip('=')):
                    raise RuntimeError(f'Original C++ provider checksum mismatch: {destination}')
                source = prefix/'Scripts'/name
                if not source.is_file(): source = prefix/name
                shutil.copyfile(source,destination)
                report['overrides'].append({'provider':distribution.metadata['Name'],
                    'path':str(destination),'original_sha256':current.hex(),
                    'selected_sha256':manifest['files'][name]['sha256']})
        report['success'] = True
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    if os.name != 'nt' or sys.prefix == sys.base_prefix:
        raise RuntimeError('C++ preparation requires the isolated Windows build venv.')
    from tempo_release import windows_signature
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root/'installer/windows-native-runtime.json').read_text())
    prepare_build_environment(manifest,root/'Output/native-environment-audit.json',verify_signature=windows_signature)


if __name__ == '__main__':
    main()
