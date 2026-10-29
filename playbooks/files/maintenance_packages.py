#!/usr/bin/env python3
"""Prepare and consume a host-local, sealed pacman/AUR archive transaction.

Only repo/aur preparation uses the network. Install never invokes a resolver or
downloads packages. PKGBUILDs have the same trust requirements as ordinary paru.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from urllib.parse import unquote, urlparse

KUBE = {'kubeadm', 'kubelet', 'kubectl'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def run(argv, cwd=None, allowed=(0,), timeout=1800):
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True,
                            timeout=timeout, env=os.environ | {'LC_ALL': 'C', 'LANG': 'C'})
    require(result.returncode in allowed,
            'Command failed: ' + ' '.join(map(str, argv)) + '\n' + result.stdout + result.stderr)
    require(not (result.returncode == 1 and result.stderr.strip()),
            'Query failed: ' + ' '.join(map(str, argv)) + '\n' + result.stderr)
    return result.stdout.strip()


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def installed():
    return dict(line.split() for line in run(['pacman', '-Q']).splitlines())


def metadata(path):
    fields = run(['pacman', '-Qp', str(path)]).split()
    require(len(fields) == 2, 'Invalid archive metadata: ' + str(path))
    return fields


def records(output):
    result = []
    for line in output.splitlines():
        fields = line.split()
        require(len(fields) == 4, 'Unexpected pacman resolution output: ' + line)
        name, version, url, repo = fields
        require(re.fullmatch(r'[A-Za-z0-9@._+:-]+', name) is not None, 'Invalid package name')
        result.append(dict(name=name, version=version, url=url, repo=repo))
    require(len({x['name'] for x in result}) == len(result), 'Duplicate package resolution')
    return result


def sync_metadata(repo, name, version):
    with tarfile.open('/var/lib/pacman/sync/' + repo + '.db') as database:
        for entry in database:
            if not entry.name.endswith('/desc'):
                continue
            fields = database.extractfile(entry).read().decode().split('\n\n')
            data = {part.splitlines()[0].strip('%'): part.splitlines()[1:]
                    for part in fields if part.strip()}
            if data.get('NAME') == [name] and data.get('VERSION') == [version]:
                return data
    raise ValueError('Pinned package disappeared from sync database: ' + name)


def archive(path, name, version, origin):
    require(path.is_file() and not path.is_symlink(), 'Missing or symlinked archive: ' + str(path))
    require(metadata(path) == [name, version], 'Archive identity mismatch: ' + name)
    return dict(name=name, version=version, path=str(path.resolve()), sha256=digest(path), origin=origin)


def repo(stage):
    require(os.geteuid() == 0, 'Repository preparation requires root')
    cache = stage / 'repo'
    cache.mkdir(mode=0o755)
    # Flatten Include directives; require trusted package signatures in every
    # repository, even when a host has a permissive per-repository override.
    config = run(['pacman-conf'])
    config = re.sub(r'(?m)^(?:SigLevel|LocalFileSigLevel)\s*=.*\n?', '', config)
    config = config.replace('[options]', '[options]\nSigLevel = PackageRequired DatabaseOptional TrustedOnly\nLocalFileSigLevel = Required TrustedOnly')
    (stage / 'pacman.conf').write_text(config)
    command = ['pacman', '--config', str(stage / 'pacman.conf')]
    before = installed()
    run(command + ['-Sy', '--noconfirm'])
    query = ['-Sup', '--noconfirm', '--print-format', '%n %v %l %r']
    selected = records(run(command + query))
    candidates = records(run(command + ['-Sp', '--print-format', '%n %v %l %r', *sorted(KUBE)]))
    targets = {x['name']: x['version'] for x in candidates if x['name'] in KUBE}
    require(set(targets) == KUBE and KUBE <= before.keys(), 'Missing Kubernetes package/candidate')
    if before['kubeadm'] != targets['kubeadm']:
        target = next(x for x in candidates if x['name'] == 'kubeadm')
        deps = sync_metadata(target['repo'], 'kubeadm', target['version']).get('DEPENDS', [])
        require(not deps or not run(['pacman', '-T', *deps], allowed=(0, 127)),
                'Target kubeadm requires new dependencies; provision separately before maintenance')
    if selected:
        run(command + ['-Suw', '--noconfirm', '--cachedir', str(cache)])
    require(records(run(command + query)) == selected, 'Repository resolution changed during download')
    archives = []
    for item in selected:
        info = sync_metadata(item['repo'], item['name'], item['version'])
        path = cache / info['FILENAME'][0]
        require(path.name == Path(unquote(urlparse(item['url']).path)).name, 'Archive filename mismatch')
        require(digest(path) == info['SHA256SUM'][0], 'Repository checksum mismatch: ' + item['name'])
        signature = Path(str(path) + '.sig')
        if info.get('PGPSIG'):
            signature.write_bytes(base64.b64decode(info['PGPSIG'][0], validate=True))
        require(signature.is_file(), 'Missing repository signature: ' + item['name'])
        run(['pacman-key', '--verify', str(signature), str(path)])
        archives.append(archive(path, item['name'], item['version'], 'repo'))
    require(installed() == before, 'Package state changed during repository staging')
    result = dict(installed=before, candidates=targets, archives=archives)
    (stage / 'repo.json').write_text(json.dumps(result))
    return result


def memory_safe(meminfo, minimum=2 * 1024 * 1024):
    values = {line.split(':')[0]: int(line.split()[1]) for line in meminfo.splitlines()
              if line.startswith(('MemAvailable:', 'MemTotal:'))}
    return values['MemAvailable'] >= max(minimum, values['MemTotal'] // 10)


def build(directory):
    # Foreground supervised process group: abort the compiler children as well
    # as makepkg, before available memory crosses the host safety reserve.
    log_path = directory / 'maintenance-build.log'
    try:
        with log_path.open('w') as log:
            require(memory_safe(Path('/proc/meminfo').read_text()), 'Insufficient available memory before AUR build')
            # Maintenance produces installable archives, without running the
            # upstream application check() suite. Source verification remains on.
            process = subprocess.Popen(['makepkg', '--noconfirm', '--force', '--nocheck'], cwd=directory,
                                       stdout=log, stderr=subprocess.STDOUT, process_group=0,
                                       env=os.environ | {'MAKEFLAGS': '-j2', 'CARGO_BUILD_JOBS': '2', 'LC_ALL': 'C'})
            deadline = time.monotonic() + 7200
            try:
                while process.poll() is None:
                    require(time.monotonic() < deadline, 'AUR build exceeded two-hour deadline')
                    require(memory_safe(Path('/proc/meminfo').read_text()), 'AUR build aborted at memory reserve')
                    time.sleep(0.5)
                require(process.returncode == 0,
                        'AUR build failed (exit ' + str(process.returncode) +
                        '); missing/new/cyclic dependencies must be provisioned separately')
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    except (ValueError, OSError) as error:
        # Bound both I/O and diagnostic size even for very large compiler logs.
        try:
            with log_path.open('rb') as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 8192))
                tail = '\n'.join(log.read(8192).decode('utf-8', errors='replace').splitlines()[-40:])
        except OSError:
            tail = '(build log unavailable)'
        raise ValueError(f'AUR package {directory.name} in {directory}: {error}\n'
                         f'Build log: {log_path}\nLast up to 40 lines / 8192 bytes:\n{tail}') from error


def cached_paru_archive(directory, name, target, recipe, clone_dir):
    """Accept a prior build only when its embedded recipe identity matches."""
    for path in sorted((clone_dir / directory.name).glob('*.pkg.tar.*')):
        if path.name.endswith('.sig') or not path.is_file() or path.is_symlink():
            continue
        try:
            if metadata(path) != [name, target]:
                continue
            info = run(['bsdtar', '-xOf', str(path), '.BUILDINFO'])
            hashes = [line.split(' = ', 1)[1] for line in info.splitlines()
                      if line.startswith('pkgbuild_sha256sum = ')]
            if hashes == [recipe['pkgbuild']]:
                return archive(path, name, target, 'aur')
        except ValueError:
            # A stale/corrupt cache entry is not prepared evidence; build the
            # freshly verified recipe instead. Never install this entry.
            continue
    return None


def aur(stage, clone_dir=None):
    require(os.geteuid() != 0, 'AUR builds must be unprivileged')
    before = installed()
    # Query uses the invoking user's actual paru config, including Devel and
    # IgnoreDevel policy. Do not override it with --nodevel or fabricate -Suw.
    output = run(['paru', '-Qua', '--color', 'never'], allowed=(0, 1))
    selected = []
    for line in output.splitlines():
        match = re.fullmatch(r'(\S+)\s+(\S+)\s+->\s+(\S+)', line)
        require(match is not None, 'Unexpected AUR update query: ' + line)
        name, old, new = match.groups()
        require(before.get(name) == old, 'AUR installed version drift: ' + name)
        selected.append((name, new))
    artifacts = []
    sources = stage / 'sources'
    sources.mkdir()
    if clone_dir is None:
        clone_dir = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'paru/clone'
    prepared = []
    for name, target in selected:
        # -G fetches PKGBUILDs, not packages and never installs build deps.
        work = sources / name
        work.mkdir()
        run(['paru', '-G', '--noconfirm', name], cwd=work)
        directories = [p.parent for p in work.glob('*/PKGBUILD')]
        require(len(directories) == 1, 'No unique fetched PKGBUILD for ' + name)
        directory = directories[0]
        info = run(['makepkg', '--printsrcinfo'], cwd=directory)
        deps = [line.split('=', 1)[1].strip() for line in info.splitlines()
                if re.match(r'\s*(?:make|check)?depends(?:_' + re.escape(os.uname().machine) + r')?\s*=', line)]
        if deps:
            missing = run(['pacman', '-T', *deps], allowed=(0, 127))
            require(not missing, 'Missing/new/cyclic AUR build dependencies for ' + name + ': ' + missing)
        prepared.append((name, target, directory, info))
    # Inspect every selected recipe's dependencies before starting any build.
    for name, target, directory, info in prepared:
        # Verify source signatures/checksums before compiling. No --syncdeps,
        # --install, dependency skipping, or host package transaction here.
        run(['makepkg', '--verifysource', '--noconfirm'], cwd=directory)
        # Reuse only our own checksum-recorded non-devel archive with identical
        # reviewed recipe metadata. Arbitrary files in paru's cache are not
        # evidence of a successful build of the freshly fetched PKGBUILD.
        cache = Path.home() / '.cache/ansible-maintenance' / name
        cache.mkdir(parents=True, exist_ok=True)
        identity = dict(pkgbuild=digest(directory / 'PKGBUILD'), srcinfo=hashlib.sha256(info.encode()).hexdigest())
        devel = target == 'latest-commit' or bool(re.search(
            r'(?m)^\s*source(?:_[^ =]+)?\s*=\s*(?:[^\s]+::)?(?:git|svn|hg|bzr|fossil)[:+]', info))
        cached = cache / 'archive.json'
        reused = False
        if not devel and cached.exists():
            previous = json.loads(cached.read_text())
            path = cache / previous['filename']
            if (previous['recipe'] == identity and path.is_file() and
                    digest(path) == previous['sha256'] and metadata(path) == [name, target]):
                # Keep cache reuse independent of makepkg's exit-13 "already
                # built" path, which is not an error in a prepared transaction.
                artifacts.append(archive(path, name, target, 'aur'))
                reused = True
        if reused:
            continue
        if not devel:
            previous = cached_paru_archive(directory, name, target, identity, clone_dir)
            if previous:
                artifacts.append(previous)
                continue
        build(directory)
        paths = run(['makepkg', '--packagelist'], cwd=directory).splitlines()
        matches = []
        for filename in paths:
            path = Path(filename)
            pkgname, version = metadata(path)
            if pkgname == name:
                # Devel queries may report "latest-commit"; freeze the actual
                # built version. Ordinary versions must match discovery.
                require(devel or version == target,
                        'AUR source version drift: ' + name)
                if devel and target != 'latest-commit':
                    require(int(run(['vercmp', version, target])) >= 0, 'AUR built version precedes advertised target: ' + name)
                require(int(run(['vercmp', version, before[name]])) >= 0, 'AUR downgrade: ' + name)
                matches.append(archive(path, name, version, 'aur'))
        require(len(matches) == 1, 'No unique built archive for ' + name)
        artifacts.extend(matches)
        source = Path(matches[0]['path'])
        shutil.copyfile(source, cache / source.name)
        cached.write_text(json.dumps(dict(recipe=identity, filename=source.name, sha256=matches[0]['sha256'])))
    require(installed() == before, 'AUR preparation changed host packages')
    result = dict(installed=before, archives=artifacts)
    (stage / 'aur.json').write_text(json.dumps(result))
    return result


def verify(stage, state=True):
    data = json.loads((stage / 'manifest.json').read_text())
    for item in data['archives']:
        path = Path(item['path'])
        require(path.parent == stage / 'sealed', 'Archive escaped sealed staging')
        require(digest(path) == item['sha256'], 'Pinned archive checksum drift: ' + item['name'])
        require(metadata(path) == [item['name'], item['version']], 'Pinned archive identity drift')
        if item['origin'] == 'repo':
            run(['pacman-key', '--verify', str(path) + '.sig', str(path)])
    if state:
        require(installed() == data['installed'], 'Installed package state drift since staging')
    return data


def seal(stage, aur_stage):
    data = json.loads((stage / 'repo.json').read_text())
    user = json.loads((aur_stage / 'aur.json').read_text())
    require(data['installed'] == user['installed'] == installed(), 'Package state drift during staging')
    data['archives'].extend(user['archives'])
    require(len({p['name'] for p in data['archives']}) == len(data['archives']), 'Repo/AUR package overlap')
    require(set(data['candidates']) == KUBE, 'Missing Kubernetes candidates')
    for name, target in data['candidates'].items():
        selected = [p for p in data['archives'] if p['name'] == name]
        require((not selected and data['installed'].get(name) == target) or
                (len(selected) == 1 and selected[0]['version'] == target and selected[0]['origin'] == 'repo'),
                'Kubernetes candidate is neither installed nor staged from repository: ' + name)
    sealed = stage / 'sealed'
    sealed.mkdir(mode=0o700)
    for item in data['archives']:
        source = Path(item['path'])
        dest = sealed / source.name
        require(not dest.exists(), 'Archive filename collision')
        shutil.copyfile(source, dest)
        dest.chmod(0o600)
        require(digest(dest) == item['sha256'], 'Archive changed while sealing')
        if item['origin'] == 'repo':
            shutil.copyfile(str(source) + '.sig', str(dest) + '.sig')
        item['path'] = str(dest)
    (stage / 'manifest.json').write_text(json.dumps(data))
    verify(stage)
    # kubeadm can be staged first only with dependencies ALREADY satisfied.
    # Never partially upgrade glibc or install new build/runtime deps to force
    # this early transaction through. Pacman performs its own check again.
    early = [p for p in data['archives'] if p['name'] == 'kubeadm']
    if early:
        require(run(['pacman', '-Up', '--noconfirm', '--print-format', '%n', early[0]['path']]).splitlines() == ['kubeadm'],
                'Early kubeadm transaction requires unstaged packages')
    # A local-file print transaction resolves dependencies/conflicts without
    # installing. It must not pull any additional repository packages.
    paths = [p['path'] for p in data['archives']]
    if paths:
        resolved = run(['pacman', '-Up', '--noconfirm', '--print-format', '%n', *paths]).splitlines()
        require(set(resolved) == {p['name'] for p in data['archives']}, 'Unstaged transaction dependency')
    return data


def install(stage, phase):
    data = verify(stage, state=False)
    current = installed()
    expected = dict(data['installed'])
    if phase == 'rest':
        for item in data['archives']:
            if item['name'] == 'kubeadm':
                expected['kubeadm'] = item['version']
    require(current == expected, 'Installed package drift before pinned transaction')
    selected = [p for p in data['archives'] if (p['name'] == 'kubeadm') == (phase == 'kubeadm')]
    if selected:
        # Repo signatures were checked above against the system trust store;
        # AUR archives remain locally built and checksum sealed. No network
        # repositories are available to this local-file transaction.
        config = run(['pacman-conf']).split('[', 2)
        require(len(config) >= 2 and config[1].startswith('options]'), 'Invalid pacman config')
        options = '[options]' + config[1].split(']', 1)[1]
        (stage / 'install.conf').write_text(options)
        run(['pacman', '--config', str(stage / 'install.conf'), '-U', '--noconfirm',
             *[p['path'] for p in selected]])
    after = installed()
    for item in selected:
        expected[item['name']] = item['version']
    require(after == expected, 'Unexpected package state after pinned transaction')
    return dict(changed=bool(selected))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['repo', 'aur', 'seal', 'verify', 'kubeadm', 'rest'])
    parser.add_argument('stage', type=Path)
    parser.add_argument('--aur-stage', type=Path)
    parser.add_argument('--aur-cache', type=Path)
    args = parser.parse_args()
    stage = args.stage.resolve()
    require(stage.is_absolute() and str(stage).startswith('/var/lib/ansible-maintenance/'), 'Use disk maintenance staging')
    if args.action == 'seal':
        result = seal(stage, args.aur_stage)
    elif args.action == 'aur':
        result = aur(stage, args.aur_cache)
    elif args.action in ('kubeadm', 'rest'):
        result = install(stage, args.action)
    else:
        result = globals()[args.action](stage)
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        sys.exit('Package staging failed: ' + str(error))
