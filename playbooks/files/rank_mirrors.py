#!/usr/bin/env python3
"""Put the mirror that really downloads fastest first in every pacman mirrorlist.

pacman takes every package from the first Server of a repository and only
moves on when that server fails, so one slow first entry stalls a whole
upgrade. cachyos-rate-mirrors forces fixed entries above its rated results.
This downloads a repository database from the first HEAD servers of each
mirrorlist named in /etc/pacman.conf and orders them by measured throughput.
"""
import os
import re
import subprocess
import sys
import time
import urllib.request

HEAD = 5
SECONDS = 3.0


def measure(url, seconds=SECONDS):
    """Bytes per second of one download, cut off after `seconds`; 0 on any error."""
    start = time.monotonic()
    received = 0
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'pacman-mirror-check'})
        with urllib.request.urlopen(request, timeout=seconds) as response:
            while time.monotonic() - start < seconds:
                chunk = response.read(65536)
                if not chunk:
                    break
                received += len(chunk)
    except (OSError, ValueError):
        return 0.0
    return received / max(time.monotonic() - start, 0.001)


def rank(lines, speeds):
    """Reorder the first len(speeds) Server lines of a mirrorlist, fastest first."""
    slots = [i for i, line in enumerate(lines) if line.startswith('Server')][:len(speeds)]
    order = sorted(range(len(slots)), key=lambda i: -speeds[i])
    result = list(lines)
    for slot, source in zip(slots, order):
        result[slot] = lines[slots[source]]
    return result


def pacman_conf(*args):
    return subprocess.run(['pacman-conf', *args], check=True, capture_output=True,
                          encoding='utf-8', errors='replace').stdout.splitlines()


def groups(config='/etc/pacman.conf'):
    """Map each included mirrorlist to the first repository that uses it."""
    result, repo = {}, None
    for line in open(config, encoding='utf-8', errors='replace'):
        section = re.fullmatch(r'\[(.+)\]', line.strip())
        include = re.fullmatch(r'Include\s*=\s*(\S+)', line.strip())
        if section:
            repo = section.group(1)
        elif include and repo != 'options':
            result.setdefault(include.group(1), repo)
    return result


def main():
    changed = False
    for path, repo in groups().items():
        lines = open(path, encoding='utf-8', errors='replace').read().splitlines()
        servers = [line.split('=', 1)[1].strip() for line in pacman_conf('-r', repo)
                   if line.startswith('Server')]
        if len(servers) != sum(line.startswith('Server') for line in lines):
            sys.exit('Server lines of ' + path + ' do not match repository ' + repo)
        head = servers[:HEAD]
        speeds = [measure(url + '/' + repo + '.files') for url in head]
        for url, speed in sorted(zip(head, speeds), key=lambda x: -x[1]):
            print('%s %7.2f MB/s %s' % (os.path.basename(path), speed / 1048576, url))
        ranked = rank(lines, speeds)
        if ranked != lines:
            temporary = path + '.rank'
            with open(temporary, 'w', encoding='utf-8') as output:
                output.write('\n'.join(ranked) + '\n')
            os.chmod(temporary, 0o644)
            os.replace(temporary, path)
            changed = True
    print('REORDERED' if changed else 'UNCHANGED')


if __name__ == '__main__':
    main()
