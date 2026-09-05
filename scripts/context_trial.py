"""Opt-in experimental context-management setting, with backup and parse checks.

Preview is default. Apply refuses a running Codex desktop on Windows. This does
not change model, effort, permission, or context/compaction limits. No API calls.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib


def candidate(raw: bytes) -> bytes:
    text = raw.decode('utf-8-sig')
    before = tomllib.loads(text)
    features = before.get('features', {})
    if not isinstance(features, dict):
        raise ValueError('features is not a table')
    if 'context_management' in features:
        value = features['context_management']
        if isinstance(value, dict) and value.get('experimental_mode') is True:
            return raw
        raise ValueError('Existing context-management configuration needs a targeted merge; unchanged')
    newline = '\r\n' if '\r\n' in text else '\n'
    addition = newline + '[features.context_management]' + newline + 'experimental_mode = true' + newline
    updated = raw + addition.encode('utf-8')
    after = tomllib.loads(updated.decode('utf-8-sig'))
    expected = copy.deepcopy(before)
    expected.setdefault('features', {})['context_management'] = {'experimental_mode': True}
    if after != expected:
        raise ValueError('Candidate changed unrelated settings')
    return updated


def assert_closed() -> None:
    if os.name != 'nt':
        raise ValueError('Automatic apply is Windows-only; use the preview on other hosts')
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    check = "@(Get-Process -Name ChatGPT -ErrorAction SilentlyContinue | Where-Object { -not $_.Path -or $_.Path -like '*\\OpenAI.Codex_*\\app\\ChatGPT.exe' }).Count"
    result = subprocess.run([str(shell), '-NoProfile', '-Command', check],
                            capture_output=True, text=True, timeout=15)
    if result.returncode or result.stdout.strip() != '0':
        raise ValueError('Codex must be fully closed before applying configuration')


@contextmanager
def exclusive_config(config: Path):
    # Deny read/write/delete sharing while validating, backing up, and updating.
    # Writing through this handle preserves the original file's Windows ACL.
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    handle = create(str(config), 0xC0000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'r+b') as stream:
        yield stream


def apply(config: Path, expected_sha256: str | None = None) -> dict:
    config = config.absolute()
    for path in (config, *config.parents):
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('Refusing a linked configuration path')
    original = config.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError('Configuration changed since preview; refusing stale activation')
    updated = candidate(original)
    if updated == original:
        return {'status': 'already_enabled'}
    assert_closed()
    backups = config.parent / 'backups'
    if backups.is_symlink() or (hasattr(backups, 'is_junction') and backups.is_junction()):
        raise ValueError('Refusing linked backup directory')
    backups.mkdir(exist_ok=True)
    with exclusive_config(config) as stream:
        if stream.read() != original:
            raise ValueError('Configuration changed during preparation; unchanged')
        backup = Path(tempfile.mkdtemp(prefix='context-management-', dir=backups)) / 'config.toml'
        with backup.open('xb') as saved:
            saved.write(original)
            saved.flush()
            os.fsync(saved.fileno())
        if backup.read_bytes() != original:
            raise ValueError('Backup verification failed; unchanged')
        assert_closed()
        try:
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read() != updated:
                raise ValueError('Post-write verification failed')
        except BaseException:
            stream.seek(0)
            stream.write(original)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
            raise
    return {'status': 'enabled', 'backup': str(backup), 'sha256': hashlib.sha256(updated).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    try:
        if args.apply:
            result = apply(args.config, args.expected_sha256)
        else:
            raw = args.config.read_bytes()
            result = {'status': 'preview', 'would_change': candidate(raw) != raw,
                      'source_sha256': hashlib.sha256(raw).hexdigest(),
                      'only_setting': 'features.context_management.experimental_mode=true'}
        print(json.dumps(result))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Never echo TOML excerpts or process output that could contain secrets.
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        print(json.dumps({'status': 'refused', 'reason': message}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
