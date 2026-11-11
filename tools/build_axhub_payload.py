#!/usr/bin/env python3
"""Create a local, explicit-allowlist AXHub source ZIP. Never uploads or deploys.

This is a packaging check, not a spending cap, platform validation, or secret
audit. Review the source before any authorized upload. No credentials or runtime
state are read, and original fixtures must match the reviewed source manifest.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ('Dockerfile', 'axhub.yaml', '.dockerignore', 'requirements.txt',
              'backend.py', 'persistence.py', 'cloud_runtime.py', 'ai_budget.py', 'supply_alternatives.py')
STATIC_FILES = ('favicon.svg', 'app.js', 'index.html', 'landing.js', 'style.css',
                'landing.html', 'workspace.css', 'landing.css', 'common-nav.css', 'common-nav.js', 'supply-alternatives.js', 'supply-alternatives.css')
SECRET_MARKERS = (
    re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
    re.compile(rb'\bsk-(?:ant-|proj-)[A-Za-z0-9_-]{20,}'),
    re.compile(rb'\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}'),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_read(root, relative):
    """Reject links and special files, including links in parent components."""
    part = PurePosixPath(relative)
    if part.is_absolute() or not part.parts or any(p in ('.', '..') for p in part.parts) or '\\' in relative:
        raise ValueError('Unsafe payload path')
    current = root
    for name in part.parts:
        current = current / name
        mode = current.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError('Symlinks are forbidden in payload: ' + relative)
    if not stat.S_ISREG(mode):
        raise ValueError('Payload entry is not a regular file: ' + relative)
    data = current.read_bytes()
    if any(marker.search(data) for marker in SECRET_MARKERS):
        raise ValueError('Credential-like content requires review: ' + relative)
    return data


def inventory(root):
    root = Path(root).resolve()
    manifest = json.loads(safe_read(root, 'evidence/source-manifest.json'))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError('Reviewed source manifest must be a nonempty list')
    payload = {name: safe_read(root, name) for name in ROOT_FILES}
    expected_original = set()
    for entry in manifest:
        name = entry.get('file')
        if not isinstance(name, str) or not name or name.startswith('/'):
            raise ValueError('Invalid original source path')
        relative = 'data/original/' + name
        if relative in expected_original:
            raise ValueError('Duplicate original source path')
        expected_original.add(relative)
        data = safe_read(root, relative)
        if digest(data) != entry.get('sha256') or len(data) != entry.get('bytes'):
            raise ValueError('Reviewed original fixture changed: ' + name)
        payload[relative] = data
    for name in STATIC_FILES:
        relative = 'static/' + name
        payload[relative] = safe_read(root, relative)
    # New or hidden entries under COPY trees require explicit review. Nothing
    # unknown in these trees is silently uploaded or silently omitted.
    for folder, expected in [('static', {'static/' + n for n in STATIC_FILES}),
                             ('data/original', expected_original)]:
        actual = set()
        for path in (root / folder).rglob('*'):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError('Symlinks are forbidden in payload: ' + relative)
            if not path.is_dir():
                actual.add(relative)
        if actual != expected:
            raise ValueError('Unreviewed or missing files in ' + folder)
    return dict(sorted(payload.items()))


def build(root, output):
    payload = inventory(root)
    output = Path(output)
    # Exclusive creation prevents clobbering an earlier artifact or a symlink.
    with output.open('xb') as stream:
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
    # Verify bytes and inventory from the actual archive, not just the inputs.
    with zipfile.ZipFile(output) as archive:
        if archive.namelist() != list(payload) or archive.testzip() is not None:
            raise ValueError('Payload ZIP verification failed')
        for name, expected in payload.items():
            if archive.read(name) != expected:
                raise ValueError('Payload byte verification failed: ' + name)
    return {
        'kind': 'local_axhub_source_payload', 'archive': str(output),
        'sha256': digest(output.read_bytes()), 'files': len(payload),
        'uncompressed_bytes': sum(map(len, payload.values())),
        'source_files': {name: {'sha256': digest(data), 'bytes': len(data)}
                         for name, data in payload.items()},
        'uploaded': False, 'deployed': False, 'model_call_made': False,
        'limitations': ['Known-pattern checks do not prove absence of all secrets.',
                       'Manifest compatibility, enforced budget, Docker build and cloud checks remain separate gates.'],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.root, args.output), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.exit(1, type(error).__name__ + ': ' + str(error) + '\n')
