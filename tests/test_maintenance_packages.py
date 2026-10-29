"""Offline package preparation contracts; no live pacman/network operations."""
import json
import base64
import hashlib
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playbooks/files'))
import maintenance_packages as packages


class PackageTests(unittest.TestCase):
    def test_seal_requires_exact_repository_candidate_or_installed_version(self):
        for case in ('installed', 'repo', 'ignored', 'wrong-version', 'aur', 'wrong-name'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                aur = stage / 'aur'
                aur.mkdir()
                before = {p: '1.37.1-1' for p in packages.KUBE}
                candidates = dict(before)
                archives = []
                if case != 'installed':
                    candidates['kubelet'] = '1.37.1-2'
                if case not in ('installed', 'ignored'):
                    name = 'unrelated' if case == 'wrong-name' else 'kubelet'
                    version = '1.37.1-3' if case == 'wrong-version' else '1.37.1-2'
                    source = stage / 'kubelet.pkg.tar.zst'
                    source.write_bytes(b'archive')
                    Path(str(source) + '.sig').write_bytes(b'signature')
                    archives = [dict(name=name, version=version, path=str(source),
                                     sha256=packages.digest(source), origin='aur' if case == 'aur' else 'repo')]
                (stage / 'repo.json').write_text(json.dumps(dict(
                    installed=before, candidates=candidates, archives=archives)))
                (aur / 'aur.json').write_text(json.dumps(dict(installed=before, archives=[])))
                with patch.object(packages, 'installed', return_value=before), patch.object(
                        packages, 'metadata', return_value=['kubelet', '1.37.1-2']), patch.object(
                        packages, 'run', side_effect=lambda argv: 'kubelet' if '-Up' in argv else ''):
                    if case in ('installed', 'repo'):
                        result = packages.seal(stage, aur)
                        self.assertEqual(result['candidates'], candidates)
                    else:
                        with self.assertRaisesRegex(ValueError, 'neither installed nor staged'):
                            packages.seal(stage, aur)
                        self.assertFalse((stage / 'manifest.json').exists())

    def test_paru_cache_requires_embedded_recipe_and_archive_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); base=root/'sample'; base.mkdir()
            path=base/'sample.pkg.tar.zst'; path.write_bytes(b'cached build')
            for embedded, expected in [('correct',True),('stale',False)]:
                with patch.object(packages,'metadata',return_value=['sample','2-1']), patch.object(packages,'run',return_value='pkgbuild_sha256sum = '+embedded):
                    result=packages.cached_paru_archive(base,'sample','2-1',{'pkgbuild':'correct'},root)
                    self.assertEqual(result is not None,expected)
                    if result: self.assertEqual(result['sha256'],packages.digest(path))

    def test_repository_download_signature_checksum_and_resolution_gates(self):
        for failure in (None, 'checksum', 'signature', 'resolution'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                calls = []
                resolutions = 0
                def run(argv, **kwargs):
                    nonlocal resolutions
                    calls.append(argv)
                    if argv == ['pacman-conf']:
                        return '[options]\nSigLevel = Optional TrustAll\n[core]\nSigLevel = Never\nServer = https://repo\n'
                    if '-Sup' in argv:
                        resolutions += 1
                        version = '3-1' if failure == 'resolution' and resolutions > 1 else '2-1'
                        return 'sample '+version+' https://repo/sample.pkg.tar.zst core'
                    if '-Sp' in argv:
                        return '\n'.join(p+' 1.37.1-1 https://repo/'+p+' core' for p in packages.KUBE)
                    if '-Suw' in argv:
                        (stage / 'repo/sample.pkg.tar.zst').write_bytes(b'archive')
                    if argv[:2] == ['pacman-key','--verify'] and failure == 'signature':
                        raise ValueError('bad signature')
                    return ''
                info = dict(FILENAME=['sample.pkg.tar.zst'],SHA256SUM=['bad' if failure == 'checksum' else hashlib.sha256(b'archive').hexdigest()],PGPSIG=[base64.b64encode(b'signature').decode()])
                with patch.object(packages.os,'geteuid',return_value=0), patch.object(packages,'run',side_effect=run), patch.object(packages,'installed',return_value={p:'1.37.1-1' for p in packages.KUBE}), patch.object(packages,'sync_metadata',return_value=info), patch.object(packages,'metadata',return_value=['sample','2-1']):
                    if failure:
                        with self.assertRaises(ValueError): packages.repo(stage)
                    else:
                        result=packages.repo(stage)
                        self.assertEqual(result['archives'][0]['name'],'sample')
                        self.assertTrue(any(c[:2]==['pacman-key','--verify'] for c in calls))
                    config=(stage/'pacman.conf').read_text()
                    self.assertIn('PackageRequired DatabaseOptional TrustedOnly',config)
                    self.assertNotIn('TrustAll',config)
                    self.assertNotIn('SigLevel = Never',config)
                    self.assertFalse(any('-U' in c or '-Syu' in c for c in calls))

    def test_aur_build_archive_cached_reuse_and_configured_devel(self):
        for devel in (False,True):
            with self.subTest(devel=devel), tempfile.TemporaryDirectory() as temp:
                work=Path(temp)
                built=[]
                verified=[]
                def run(argv,cwd=None,**kwargs):
                    if argv[:2]==['paru','-Qua']:
                        return 'sample 1-1 -> '+('latest-commit' if devel else '2-1')
                    if argv[:2]==['paru','-G']:
                        (cwd/'sample').mkdir()
                        (cwd/'sample/PKGBUILD').write_text('pkgname=sample\npkgver=2')
                        return ''
                    if '--printsrcinfo' in argv: return 'pkgbase = sample\n\tpkgver = 2'
                    if '--verifysource' in argv:
                        verified.append(argv)
                        return ''
                    if '--packagelist' in argv: return str(cwd/'sample.pkg.tar.zst')
                    if argv[0]=='vercmp': return '1'
                    self.fail('Unexpected command: '+repr(argv))
                def build(directory):
                    built.append(directory)
                    (directory/'sample.pkg.tar.zst').write_bytes(b'compiled archive')
                with patch.object(packages.os,'geteuid',return_value=1000), patch.object(packages.Path,'home',return_value=work), patch.object(packages,'installed',return_value={'sample':'1-1'}), patch.object(packages,'run',side_effect=run), patch.object(packages,'metadata',return_value=['sample','2-1']), patch.object(packages,'build',side_effect=build):
                    for number in range(2):
                        stage=work/str(number); stage.mkdir()
                        result=packages.aur(stage)
                        self.assertEqual(result['archives'][0]['version'],'2-1')
                        self.assertEqual(result['archives'][0]['sha256'],hashlib.sha256(b'compiled archive').hexdigest())
                self.assertEqual(len(built),2 if devel else 1)
                self.assertEqual(verified, [['makepkg', '--verifysource', '--noconfirm']] * 2)

    def test_empty_query_does_not_hide_network_failure(self):
        result = subprocess.CompletedProcess(['paru'], 1, '', 'AUR connection failed')
        with patch.object(packages.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Query failed'):
                packages.run(['paru', '-Qua'], allowed=(0, 1))

    def test_resolution_is_exact_and_rejects_ambiguous_output(self):
        self.assertEqual(packages.records('kubeadm 1.38.0-1 https://repo/k.pkg core')[0]['version'], '1.38.0-1')
        for text in ('warning: unexpected output', 'x 1 url core\nx 2 url core'):
            with self.assertRaises(ValueError):
                packages.records(text)

    def test_archive_metadata_and_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.pkg.tar.zst'
            path.write_bytes(b'archive')
            with patch.object(packages, 'metadata', return_value=['test', '1-1']):
                result = packages.archive(path, 'test', '1-1', 'aur')
                self.assertEqual(result['sha256'], packages.digest(path))
                with self.assertRaisesRegex(ValueError, 'identity'):
                    packages.archive(path, 'other', '1-1', 'aur')
            link = Path(temp) / 'link'
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, 'symlinked'):
                packages.archive(link, 'test', '1-1', 'aur')

    def manifest(self, stage, origin='repo'):
        (stage / 'sealed').mkdir()
        path = stage / 'sealed/kubeadm.pkg.tar.zst'
        path.write_bytes(b'archive')
        data = dict(installed={'kubeadm': '1.37.1-1'}, archives=[dict(
            name='kubeadm', version='1.38.0-1', path=str(path), sha256=packages.digest(path), origin=origin)])
        (stage / 'manifest.json').write_text(json.dumps(data))
        return data, path

    def test_pinned_source_and_installed_drift_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            data, path = self.manifest(stage)
            with patch.object(packages, 'metadata', return_value=['kubeadm', '1.38.0-1']), patch.object(packages, 'run', return_value='') as run, patch.object(packages, 'installed', return_value=data['installed']):
                packages.verify(stage)
                self.assertEqual(run.call_args.args[0][0:2], ['pacman-key', '--verify'])
                path.write_bytes(b'drift')
                with self.assertRaisesRegex(ValueError, 'checksum drift'):
                    packages.install(stage, 'kubeadm')
            path.write_bytes(b'archive')
            with patch.object(packages, 'metadata', return_value=['kubeadm', '1.38.0-1']), patch.object(packages, 'run', return_value=''), patch.object(packages, 'installed', return_value={'kubeadm': '1.38.1-1'}):
                with self.assertRaisesRegex(ValueError, 'state drift'):
                    packages.verify(stage)

    def test_signature_failure_prevents_install(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            self.manifest(stage)
            with patch.object(packages, 'metadata', return_value=['kubeadm', '1.38.0-1']), patch.object(packages, 'run', side_effect=ValueError('bad signature')) as run:
                with self.assertRaisesRegex(ValueError, 'bad signature'):
                    packages.install(stage, 'kubeadm')
                self.assertEqual(run.call_count, 1)

    def test_install_uses_only_pinned_local_files_and_no_sync_resolver(self):
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            data, path = self.manifest(stage, 'aur')
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                if argv == ['pacman-conf']:
                    return '[options]\nLocalFileSigLevel = Optional\n[core]\nServer = https://example.invalid\n'
                return ''
            with patch.object(packages, 'metadata', return_value=['kubeadm', '1.38.0-1']), patch.object(packages, 'installed', side_effect=[data['installed'], {'kubeadm': '1.38.0-1'}]), patch.object(packages, 'run', side_effect=run):
                self.assertTrue(packages.install(stage, 'kubeadm')['changed'])
            self.assertEqual(calls[-1], ['pacman', '--config', str(stage / 'install.conf'), '-U', '--noconfirm', str(path)])
            self.assertNotIn('[core]', (stage / 'install.conf').read_text())

    def test_missing_and_cyclic_build_dependencies_fail_before_build(self):
        for missing in ('new-library>=2', 'other-aur-cycle'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                stage = Path(temp)
                def run(argv, cwd=None, **kwargs):
                    if argv[0] == 'paru' and '-Qua' in argv:
                        return 'sample 1-1 -> 2-1'
                    if argv[0] == 'paru' and '-G' in argv:
                        (cwd / 'sample').mkdir()
                        (cwd / 'sample/PKGBUILD').write_text('pkgname=sample')
                        return ''
                    if argv == ['makepkg', '--printsrcinfo']:
                        return 'pkgbase = sample\n\tmakedepends = ' + missing
                    if argv[:2] == ['pacman', '-T']:
                        return missing
                    self.fail('Unexpected command: ' + repr(argv))
                with patch.object(packages.os, 'geteuid', return_value=1000), patch.object(packages, 'installed', return_value={'sample': '1-1'}), patch.object(packages, 'run', side_effect=run), patch.object(packages, 'build') as build:
                    with self.assertRaisesRegex(ValueError, 'Missing/new/cyclic'):
                        packages.aur(stage)
                    build.assert_not_called()

    def test_all_aur_dependency_checks_precede_first_build(self):
        with tempfile.TemporaryDirectory() as temp:
            def run(argv,cwd=None,**kwargs):
                if argv[:2]==['paru','-Qua']: return 'first 1-1 -> 2-1\nsecond 1-1 -> 2-1'
                if argv[:2]==['paru','-G']:
                    (cwd/argv[-1]).mkdir()
                    (cwd/argv[-1]/'PKGBUILD').write_text('recipe')
                    return ''
                if '--printsrcinfo' in argv:
                    return 'pkgbase = '+cwd.name+ ('\n\tmakedepends = missing' if cwd.name=='second' else '')
                if argv[:2]==['pacman','-T']: return 'missing'
                self.fail('Expensive work before all dependency checks: '+repr(argv))
            with patch.object(packages.os,'geteuid',return_value=1000), patch.object(packages,'installed',return_value={'first':'1-1','second':'1-1'}), patch.object(packages,'run',side_effect=run), patch.object(packages,'build') as build:
                with self.assertRaisesRegex(ValueError,'Missing/new/cyclic'):
                    packages.aur(Path(temp))
                build.assert_not_called()

    def test_unchanged_aur_does_not_fetch_or_build(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(packages.os, 'geteuid', return_value=1000), patch.object(packages, 'installed', return_value={}), patch.object(packages, 'run', return_value='') as run, patch.object(packages, 'build') as build:
            self.assertEqual(packages.aur(Path(temp))['archives'], [])
            self.assertEqual(run.call_args.args[0], ['paru', '-Qua', '--color', 'never'])
            build.assert_not_called()

    def test_memory_reserve_threshold(self):
        self.assertFalse(packages.memory_safe('MemTotal: 8000000 kB\nMemAvailable: 2097151 kB'))
        self.assertTrue(packages.memory_safe('MemTotal: 8000000 kB\nMemAvailable: 2097152 kB'))
        self.assertFalse(packages.memory_safe('MemTotal: 64000000 kB\nMemAvailable: 6399999 kB'))

    def test_build_argv_and_supervision_preserve_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            process = Mock(returncode=0)
            process.poll.side_effect = [None, 0, 0]
            with patch.object(packages.subprocess, 'Popen', return_value=process) as popen, patch.object(packages, 'memory_safe', return_value=True), patch.object(packages.time, 'monotonic', side_effect=[100, 101]), patch.object(packages.time, 'sleep') as sleep, patch.object(packages.os, 'killpg') as kill:
                packages.build(work)
            self.assertEqual(popen.call_args.args[0], ['makepkg', '--noconfirm', '--force', '--nocheck'])
            options = popen.call_args.kwargs
            self.assertEqual(options['cwd'], work)
            self.assertEqual(options['process_group'], 0)
            self.assertEqual(options['stderr'], subprocess.STDOUT)
            self.assertEqual(options['stdout'].name, str(work / 'maintenance-build.log'))
            self.assertEqual(options['env']['MAKEFLAGS'], '-j2')
            self.assertEqual(options['env']['CARGO_BUILD_JOBS'], '2')
            sleep.assert_called_once_with(0.5)
            kill.assert_not_called()
            process.wait.assert_not_called()

    def test_build_watchdog_diagnostics_and_process_group_cleanup(self):
        for reason, memory, times in (
                ('two-hour deadline', [True], [100, 7300]),
                ('memory reserve', [True, False], [100, 101])):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                work = Path(temp) / 'sofka-git'
                work.mkdir()
                process = Mock(pid=1234)
                process.poll.return_value = None
                def popen(*args, **kwargs):
                    kwargs['stdout'].write('compiler diagnostic\n')
                    process.wait.side_effect = lambda: kwargs['stdout'].write('cleanup finished\n')
                    return process
                with patch.object(packages.subprocess, 'Popen', side_effect=popen), patch.object(packages, 'memory_safe', side_effect=memory), patch.object(packages.time, 'monotonic', side_effect=times), patch.object(packages.time, 'sleep') as sleep, patch.object(packages.os, 'killpg') as kill:
                    with self.assertRaisesRegex(ValueError, reason) as caught:
                        packages.build(work)
                message = str(caught.exception)
                for expected in ('sofka-git', str(work), str(work / 'maintenance-build.log'), 'compiler diagnostic', 'cleanup finished'):
                    self.assertIn(expected, message)
                kill.assert_called_once_with(1234, signal.SIGKILL)
                process.wait.assert_called_once_with()
                sleep.assert_not_called()

    def test_build_preflight_and_launch_errors_have_context(self):
        for safe, reason in ((False, 'Insufficient available memory'), (True, 'cannot launch makepkg')):
            with self.subTest(safe=safe), tempfile.TemporaryDirectory() as temp:
                work = Path(temp)
                with patch.object(packages, 'memory_safe', return_value=safe), patch.object(packages.subprocess, 'Popen', side_effect=OSError('cannot launch makepkg')) as popen:
                    with self.assertRaisesRegex(ValueError, reason) as caught:
                        packages.build(work)
                self.assertIn(str(work), str(caught.exception))
                self.assertIn('maintenance-build.log', str(caught.exception))
                self.assertEqual(popen.call_count, int(safe))

    def test_failed_build_includes_bounded_log_tail(self):
        for output in ('omitted\n' + 'compiler detail\n' * 50 + 'final failure\n',
                       'omitted\n' + 'x' * 20000 + '\nfinal failure\n'):
            with self.subTest(size=len(output)), tempfile.TemporaryDirectory() as temp:
                work = Path(temp)
                process = Mock(returncode=7)
                process.poll.return_value = 7
                def popen(*args, **kwargs):
                    kwargs['stdout'].write(output)
                    return process
                with patch.object(packages.subprocess, 'Popen', side_effect=popen), patch.object(packages, 'memory_safe', return_value=True), patch.object(packages.os, 'killpg') as kill:
                    with self.assertRaisesRegex(ValueError, 'AUR build failed') as caught:
                        packages.build(work)
                message = str(caught.exception)
                self.assertIn(str(work), message)
                self.assertIn('exit 7', message)
                self.assertIn('maintenance-build.log', message)
                tail = message.split('Last up to 40 lines / 8192 bytes:\n', 1)[1]
                self.assertIn('final failure', tail)
                self.assertNotIn('omitted', tail)
                self.assertLessEqual(len(tail.encode()), 8192)
                self.assertLessEqual(len(tail.splitlines()), 40)
                self.assertEqual((work / 'maintenance-build.log').read_text(), output)
                kill.assert_not_called()
                process.wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
