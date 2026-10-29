"""Explicit Windows package variants; never replace a lockfile during a build."""
import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class BuildProfile:
    key: str
    lock: str
    source_manifest: str
    torch_version: str
    torchvision_version: str
    cuda_version: str | None


PROFILES = {
    'cpu': BuildProfile('cpu', 'requirements-windows.lock', 'installer/dependency-sources.json',
        '2.14.1+cpu', '0.29.1+cpu', None),
    'nvidia-cu126': BuildProfile('nvidia-cu126', 'requirements-windows-nvidia.lock',
        'installer/dependency-sources-nvidia.json',
        '2.14.1+cu126', '0.29.1+cu126', '12.6'),
}


def build_profile(name=None):
    name = name if name is not None else os.environ.get('TEMPO_WINDOWS_PROFILE', 'cpu')
    try:
        return PROFILES[name]
    except (KeyError, TypeError):
        raise ValueError('Unknown Windows build profile; choose cpu or nvidia-cu126.') from None


def validate_torch_build(profile, torch_version, torchvision_version, cuda_version):
    if (str(torch_version) != profile.torch_version
            or str(torchvision_version) != profile.torchvision_version
            or cuda_version != profile.cuda_version):
        raise RuntimeError('PyTorch runtime does not match the selected Windows build profile.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=tuple(PROFILES))
    parser.add_argument('--github-output', type=Path)
    args = parser.parse_args()
    selected = build_profile(args.profile)
    values = asdict(selected)
    if args.github_output:
        with args.github_output.open('a', encoding='utf-8') as stream:
            for key, value in values.items():
                stream.write(f'{key}={value if value is not None else ""}\n')
    print(json.dumps(values, indent=2))


if __name__ == '__main__':
    main()
