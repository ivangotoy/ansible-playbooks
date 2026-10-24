#!/usr/bin/env python3
"""Conservative, lossless containerd v2/v3 editor. Requires Python >= 3.11."""
import copy
import fnmatch
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib


def validate_imports(data, config_path):
    imports = data.get('imports', [])
    if not isinstance(imports, list):
        raise ValueError('Imports must be a list of optional glob strings')
    for pattern in imports:
        # Only simple filename globs have unambiguous shared Python/Go semantics.
        if (not isinstance(pattern, str) or not pattern or config_path is None
                or any(c in pattern for c in '[]\\{}') or '**' in pattern
                or any(ord(c) < 32 for c in pattern)):
            raise ValueError('Unsupported import glob')
        target = Path(pattern)
        if (not any(c in target.name for c in '*?')
                or any(c in str(target.parent) for c in '*?')
                or '..' in target.parts or pattern.endswith('/')):
            raise ValueError('Imports require simple optional filename globs')
        if not target.is_absolute():
            target = Path(config_path).absolute().parent / target
        try:
            # Unlike glob.glob, scandir propagates permission and other I/O errors;
            # fnmatch also includes dotfiles, as containerd's Go glob does.
            with os.scandir(target.parent) as entries:
                if any(fnmatch.fnmatchcase(entry.name, target.name) for entry in entries):
                    raise ValueError('Imported configuration is unsupported')
        except FileNotFoundError:
            pass


def tune(source, config_path=None):
    data = tomllib.loads(source)
    validate_imports(data, config_path)
    version = data.get('version')
    if version == 2:
        runtime = images = 'io.containerd.grpc.v1.cri'
    elif version == 3:
        runtime, images = 'io.containerd.cri.v1.runtime', 'io.containerd.cri.v1.images'
    else:
        raise ValueError('Expected containerd config version 2 or 3')
    expected = copy.deepcopy(data)
    plugins = expected['plugins']
    runtimes = plugins[runtime]['containerd']['runtimes']
    targets = {}
    for name, settings in runtimes.items():
        if settings.get('runtime_type') != 'io.containerd.runc.v2':
            raise ValueError('Unsupported runtime: ' + name)
        options = settings['options']
        if 'SystemdCgroup' in options and type(options['SystemdCgroup']) is not bool:
            raise ValueError('SystemdCgroup must be boolean')
        options['SystemdCgroup'] = True
        targets[('plugins', runtime, 'containerd', 'runtimes', name, 'options')] = ('SystemdCgroup', 'true')
    if not runtimes:
        raise ValueError('No runtimes found')
    image_settings = plugins[images]
    if 'max_concurrent_downloads' in image_settings and type(image_settings['max_concurrent_downloads']) is not int:
        raise ValueError('max_concurrent_downloads must be integer')
    image_settings['max_concurrent_downloads'] = 10
    targets[('plugins', images)] = ('max_concurrent_downloads', '10')
    transfer = 'io.containerd.transfer.v1.local'
    if version == 3:
        # Containerd 2.x can use the transfer service instead of local CRI pulls.
        transfer_settings = plugins[transfer]
        if 'max_concurrent_downloads' in transfer_settings and type(transfer_settings['max_concurrent_downloads']) is not int:
            raise ValueError('transfer max_concurrent_downloads must be integer')
        transfer_settings['max_concurrent_downloads'] = 10
        targets[('plugins', transfer)] = ('max_concurrent_downloads', '10')
    # Parse each header with TOML itself; never guess a quoted/dotted key's scope.
    lines = source.splitlines(keepends=True)
    sections = {}
    current = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith('['):
            header = tomllib.loads(line)
            path = []
            while isinstance(header, dict) and len(header) == 1:
                key, header = next(iter(header.items()))
                path.append(key)
            current = tuple(path)
            if current in sections:
                raise ValueError('Ambiguous section')
            sections[current] = [index + 1, index + 1]
        elif current is not None:
            sections[current][1] = index + 1
    edits = []
    for path, (key, value) in targets.items():
        if path not in sections:
            raise ValueError('Required explicit table missing: ' + repr(path))
        start, end = sections[path]
        matches = []
        pattern = re.compile(r'^(\s*)(?:' + key + r'|"' + key + r'"|\x27' + key + r'\x27)(\s*=\s*)(true|false|[0-9]+)(\s*(?:#.*)?)(\r?\n)?$')
        for index in range(start, end):
            match = pattern.fullmatch(lines[index])
            if match:
                matches.append(index)
                edits.append((index, index + 1, match[1] + key + match[2] + value + match[4] + (match[5] or '')))
        if not matches:
            edits.append((start, start, key + ' = ' + value + '\n'))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    result = ''.join(lines)
    if tomllib.loads(result) != expected:
        raise ValueError('Unsupported syntax or unintended semantic change')
    # Preserve even unusual formatting when the values already match.
    return source if data == expected else result


def apply(path, validate):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Configuration must be a regular file')
    original = path.read_bytes()
    result = tune(original.decode(), path).encode()
    validate(path)
    if result == original:
        validate_imports(tomllib.loads(original.decode()), path)
        return False
    stat = path.stat()
    fd, candidate = tempfile.mkstemp(prefix='.containerd-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copystat(path, candidate)
        os.chown(candidate, stat.st_uid, stat.st_gid)
        validate(Path(candidate))
        if path.read_bytes() != original:
            raise ValueError('Configuration changed during validation')
        validate_imports(tomllib.loads(original.decode()), path)
        backup_fd, backup = tempfile.mkstemp(prefix=path.name + '.backup-', dir=path.parent)
        os.close(backup_fd)
        shutil.copy2(path, backup)
        os.chown(backup, stat.st_uid, stat.st_gid)
        os.replace(candidate, path)
        return True
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)


def native_validate(path):
    subprocess.run(['containerd', '--config', str(path), 'config', 'dump'],
                   check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == '__main__':
    try:
        print('changed' if apply(sys.argv[1], native_validate) else 'ok')
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        sys.exit('Containerd config unchanged: ' + str(error))
