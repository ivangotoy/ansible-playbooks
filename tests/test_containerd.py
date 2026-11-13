"""Offline tests of the containerd tuning helper and its playbook task."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'playbooks/files' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tuning = load('tune_containerd')


def config(version=2, quote="'", value='false'):
    runtime = 'io.containerd.grpc.v1.cri' if version == 2 else 'io.containerd.cri.v1.runtime'
    images = runtime if version == 2 else 'io.containerd.cri.v1.images'
    text = f'''version = {version}
# Keep this comment and unrelated value
[unrelated]
max_concurrent_downloads = 3
SystemdCgroup = true
[plugins.{quote}{images}{quote}]
max_concurrent_downloads = 3 # keep downloads comment
'''
    if version == 3:
        text += "[plugins.'io.containerd.transfer.v1.local']\nmax_concurrent_downloads = 3\n"
    for name in ('runc', 'another'):
        text += f'''[plugins.{quote}{runtime}{quote}.containerd.runtimes.{name}]
runtime_type = "io.containerd.runc.v2"
[plugins.{quote}{runtime}{quote}.containerd.runtimes.{name}.options]
  SystemdCgroup = {value} # keep runtime comment
'''
    return text


class ContainerdTests(unittest.TestCase):
    def test_common_layouts_scopes_comments_and_idempotence(self):
        for version in (2, 3):
            for quote in ('"', "'"):
                with self.subTest(version=version, quote=quote):
                    original = config(version, quote)
                    result = tuning.tune(original)
                    parsed = tuning.tomllib.loads(result)
                    self.assertEqual(parsed['unrelated']['max_concurrent_downloads'], 3)
                    self.assertEqual(result.count('SystemdCgroup = true'), 3)
                    self.assertIn('# keep downloads comment', result)
                    self.assertEqual(result.count('# keep runtime comment'), 2)
                    self.assertEqual(tuning.tune(result), result)

    def test_absent_scoped_keys_are_added_once(self):
        original = config().replace('  SystemdCgroup = false # keep runtime comment\n', '')
        self.assertEqual(tuning.tune(original).count('SystemdCgroup = true'), 3)

    def test_reject_duplicate_false_and_true(self):
        with self.assertRaises(ValueError):
            tuning.tune(config().replace('SystemdCgroup = false', 'SystemdCgroup = false\nSystemdCgroup = true', 1))

    def test_reject_unsupported_missing_malformed_and_imports(self):
        cases = [config().replace('version = 2', 'version = 1'), 'version = 2\n',
                 config() + 'broken = [\n', 'imports = ["other.toml"]\n' + config(),
                 config().replace('io.containerd.runc.v2', 'other'),
                 config().replace('SystemdCgroup = false', 'SystemdCgroup = "false"')]
        for source in cases:
            with self.subTest(source=source[:30]), self.assertRaises((ValueError, KeyError)):
                tuning.tune(source)

    def test_atomic_backup_and_no_rewrite_when_unchanged(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(config())
            original = path.read_bytes()
            seen = []
            self.assertTrue(tuning.apply(path, lambda p: seen.append(p.read_text())))
            self.assertEqual(len(seen), 2)
            self.assertEqual(next(Path(directory).glob('*.backup-*')).read_bytes(), original)
            identity = (path.stat().st_ino, path.stat().st_mtime_ns)
            self.assertFalse(tuning.apply(path, lambda p: None))
            self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), identity)

    def test_empty_optional_import_globs(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            path = Path(directory) / 'config.toml'
            imports = Path(directory) / 'conf.d'
            imports.mkdir()
            for pattern in ('conf.d/*.toml', str(imports / '*.toml'), 'missing/?.toml'):
                with self.subTest(pattern=pattern):
                    source = 'imports = ' + json.dumps([pattern]) + '\n' + config(3)
                    result = tuning.tune(source, path)
                    self.assertEqual(tuning.tune(result, path), result)
                    path.write_text(source)
                    seen = []
                    self.assertTrue(tuning.apply(path, lambda p: seen.append(p)))
                    self.assertEqual(len(seen), 2)
                    seen.clear()
                    identity = (path.stat().st_ino, path.stat().st_mtime_ns)
                    self.assertFalse(tuning.apply(path, lambda p: seen.append(p)))
                    self.assertEqual(seen, [path])
                    self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), identity)

    def test_imports_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            path = Path(directory) / 'config.toml'
            imports = Path(directory) / 'conf.d'
            imports.mkdir()
            (imports / '.hidden.toml').write_text('imports = ["../config.toml"]\n')
            cases = [['conf.d/*.toml'], [str(imports / '*.toml')], ['missing.toml'],
                     ['conf.d/.hidden.toml'], ['**/*.toml'], ['*/?.toml'],
                     ['conf.d/[ab].toml'], ['conf.d/[.toml'], ['conf.d/\\*.toml'],
                     ['conf.d/{a,b}*.toml'], ['../*.toml'], ['conf.d/*/'],
                     [''], [42], [False], '*.toml', '', False, 0, {}]
            for value in cases:
                with self.subTest(imports=value), self.assertRaises(ValueError):
                    tuning.tune('imports = ' + json.dumps(value) + '\n' + config(3), path)
            with self.assertRaises(ValueError):
                tuning.tune('imports = ["conf.d/*.toml"]\n' + config(3))
            with patch.object(tuning.os, 'scandir', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    tuning.tune('imports = ["conf.d/*.toml"]\n' + config(3), path)

    def test_import_arriving_during_validation_keeps_original(self):
        for arrival in ('original', 'candidate', 'unchanged'):
            with self.subTest(arrival=arrival), tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
                path = Path(directory) / 'config.toml'
                imports = Path(directory) / 'conf.d'
                imports.mkdir()
                source = 'imports = ["conf.d/*.toml"]\n' + config(3)
                if arrival == 'unchanged':
                    source = tuning.tune(source, path)
                path.write_text(source)
                original = path.read_bytes()
                def validate(candidate):
                    if (candidate == path) == (arrival != 'candidate'):
                        (imports / 'arrived.toml').write_text('version = 3\n')
                with self.assertRaisesRegex(ValueError, 'Imported configuration'):
                    tuning.apply(path, validate)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(set(Path(directory).iterdir()), {path, imports})

    def test_failed_native_validation_keeps_original(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(config())
            original = path.read_bytes()
            def validate(candidate):
                if candidate != path:
                    raise ValueError('native validation failed')
            with self.assertRaisesRegex(ValueError, 'native validation'):
                tuning.apply(path, validate)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_native_validation_command_is_read_only_and_bounded(self):
        with patch.object(tuning.subprocess, 'run') as run:
            tuning.native_validate(Path('/config.toml'))
            self.assertEqual(run.call_args.args[0], ['containerd', '--config', '/config.toml', 'config', 'dump'])
            self.assertTrue(run.call_args.kwargs['check'])
            self.assertEqual(run.call_args.kwargs['timeout'], 30)


class PlaybookTests(unittest.TestCase):
    def test_containerd_final_status_changed_when(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        loader = DataLoader()
        plays = loader.load_from_file(str(ROOT / 'playbooks/cluster-perf-tuning.yml'), trusted_as_template=True)
        task = next(task for play in plays for task in play.get('tasks', [])
                    if task.get('register') == 'containerd_patch')
        for override in ('failed_when', 'ignore_errors'):
            self.assertNotIn(override, task)
        cases = [
            ('changed', True),
            ('ok', False),
            ('WARN deprecated key\nWARN unknown key\nchanged', True),
            ('WARN changed configuration key\nok', False),
            ('changed\nWARN unknown key', False),
            ('', False),
        ]
        for stdout, expected in cases:
            for newline in ('\n', '\r\n'):
                with self.subTest(stdout=stdout, newline=newline):
                    output = stdout.replace('\n', newline) + newline if stdout else ''
                    templar = Templar(loader=loader, variables={'containerd_patch': {'stdout': output}})
                    self.assertIs(templar.evaluate_conditional(task['changed_when']), expected)
