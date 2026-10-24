"""Synthetic offline gates, not evidence of successful live maintenance."""
import copy
import json
import subprocess
import datetime
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PYTHON = '/home/ivan/ansible-learning-new/bin/python'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'playbooks/files' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tuning = load('tune_containerd')
health = load('maintenance_health')


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


def fixture():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ready = [{'type': 'Ready', 'status': 'True'}]
    data = {'node': 'node1', 'version': {'serverVersion': {'gitVersion': 'v1.37.1'}},
            'resources': {'items': [
                {'kind': 'Node', 'metadata': {'name': 'node1'}, 'spec': {},
                 'status': {'conditions': ready, 'nodeInfo': {'kubeletVersion': 'v1.37.1', 'bootID': 'old'}}},
                {'kind': 'Lease', 'metadata': {'name': 'node1', 'namespace': 'kube-node-lease'},
                 'spec': {'renewTime': now}},
                {'kind': 'Pod', 'metadata': {'name': 'kured-pod', 'namespace': 'kured', 'ownerReferences': [{'uid': 'kured-id', 'controller': True}]},
                 'spec': {'nodeName': 'node1'}, 'status': {'phase': 'Running', 'conditions': ready}},
                {'kind': 'DaemonSet', 'metadata': {'name': 'kured', 'namespace': 'kured', 'uid': 'kured-id'},
                 'status': {'observedGeneration': 1, 'desiredNumberScheduled': 1, 'numberReady': 1, 'updatedNumberScheduled': 1, 'numberAvailable': 1},
                 'spec': {'template': {'spec': {'hostPID': True, 'containers': [
                     {'name': 'kured', 'image': 'ghcr.io/kubereboot/kured:1.23.0',
                      'command': ['/usr/bin/kured'], 'securityContext': {'privileged': True, 'readOnlyRootFilesystem': True},
                      'env': [{'name': 'KURED_NODE_ID', 'valueFrom': {'fieldRef': {'apiVersion': 'v1', 'fieldPath': 'spec.nodeName'}}}],
                      'args': ['--reboot-sentinel=/sentinel/reboot-required', '--period=1m', '--ds-name=kured', '--ds-namespace=kured',
                               '--metrics-port=8080', '--drain-timeout=10m', '--reboot-command=/bin/systemctl reboot',
                               '--time-zone=Europe/Sofia', '--annotate-nodes=true', '--prefer-no-schedule-taint=weave.works/kured-node-reboot',
                               '--log-format=text', '--concurrency=1'],
                      'volumeMounts': [{'name': 'sentinel', 'mountPath': '/sentinel', 'readOnly': True}]}],
                     'volumes': [{'name': 'sentinel', 'hostPath': {'path': '/var/lib/kured'}}]}}}}
            ]}}

    data['resources']['items'][2]['spec'].update(copy.deepcopy(data['resources']['items'][3]['spec']['template']['spec']))
    return data


class HealthTests(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(health.check(fixture())['boot'], 'old')

    def test_unready(self):
        data = fixture()
        data['resources']['items'][0]['status']['conditions'][0]['status'] = 'False'
        with self.assertRaisesRegex(ValueError, 'not Ready'):
            health.check(data)

    def test_incompatible_versions(self):
        data = fixture()
        data['version']['serverVersion']['gitVersion'] = 'v1.38.0'
        with self.assertRaisesRegex(ValueError, 'same minor'):
            health.check(data)

    def test_preexisting_cordon(self):
        data = fixture()
        data['resources']['items'][0]['spec']['unschedulable'] = True
        with self.assertRaisesRegex(ValueError, 'Cordoned'):
            health.check(data)

    def returned(self, reboot=True):
        data = fixture()
        data.update(baseline=health.check(data), reboot=reboot,
                    after=(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)).isoformat())
        return data

    def test_pending_reboot_unchanged_boot_id_never_passes(self):
        data = self.returned()
        for _ in range(60):
            with self.assertRaisesRegex(ValueError, 'boot-ID change'):
                health.check(data)

    def test_successful_reboot_return(self):
        data = self.returned()
        data['resources']['items'][0]['status']['nodeInfo']['bootID'] = 'new'
        self.assertEqual(health.check(data)['boot'], 'new')

    def test_no_reboot_required(self):
        health.check(self.returned(False))

    def test_stale_or_premaintenance_lease(self):
        for seconds in (2, 120):
            data = self.returned(False)
            data['after'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            data['resources']['items'][1]['spec']['renewTime'] = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()
            with self.assertRaisesRegex(ValueError, 'lease'):
                health.check(data)

    def test_unhealthy_workload(self):
        data = fixture()
        data['resources']['items'][2]['status']['phase'] = 'Pending'
        with self.assertRaisesRegex(ValueError, 'Workload unhealthy'):
            health.check(data)

    def test_kured_without_host_reboot_privileges_rejected(self):
        data = fixture()
        data['resources']['items'][3]['spec']['template']['spec']['hostPID'] = False
        with self.assertRaisesRegex(ValueError, 'hostPID'):
            health.check(data)

    def test_unsafe_kured_flags_rejected(self):
        data = fixture()
        data['resources']['items'][3]['spec']['template']['spec']['containers'][0]['args'].append('--force-reboot')
        with self.assertRaisesRegex(ValueError, 'Unsupported kured flags'):
            health.check(data)

    def test_downward_api_node_id_only(self):
        for value in ({'value': 'node1'}, {'valueFrom': {'fieldRef': {'fieldPath': 'metadata.name'}}},
                      {'valueFrom': {'secretKeyRef': {'name': 'bad', 'key': 'bad'}}}):
            data = fixture()
            data['resources']['items'][3]['spec']['template']['spec']['containers'][0]['env'] = [dict(name='KURED_NODE_ID', **value)]
            with self.assertRaisesRegex(ValueError, 'environment'):
                health.check(data)
        for flag in ('--node-id=node1', '--force-reboot=true', '--ds-namespace=kube-system'):
            data = fixture()
            data['resources']['items'][3]['spec']['template']['spec']['containers'][0]['args'].append(flag)
            with self.assertRaisesRegex(ValueError, 'flags'):
                health.check(data)

    def test_wrong_running_pod_and_incomplete_rollout(self):
        for key, value in [('args', ['--force-reboot']), ('image', 'old:image'), ('env', []),
                           ('securityContext', {'privileged': False}), ('volumeMounts', [])]:
            data = fixture()
            data['resources']['items'][2]['spec']['containers'][0][key] = value
            with self.assertRaisesRegex(ValueError, 'Running kured'):
                health.check(data)
        data = fixture()
        data['resources']['items'][3]['status']['updatedNumberScheduled'] = 0
        with self.assertRaisesRegex(ValueError, 'rollout'):
            health.check(data)

    def test_namespace_and_read_only_host_mount(self):
        data = fixture()
        data['resources']['items'][2]['metadata']['namespace'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'target'):
            health.check(data)
        for key, value in [('readOnly', False), ('subPathExpr', 'other'), ('mountPath', '/wrong')]:
            data = fixture()
            data['resources']['items'][3]['spec']['template']['spec']['containers'][0]['volumeMounts'][0][key] = value
            with self.assertRaisesRegex(ValueError, 'mount'):
                health.check(data)

    def test_read_only_stdin_preflight(self):
        result = subprocess.run([PYTHON, '-B', str(ROOT / 'playbooks/files/maintenance_health.py')],
                                input=json.dumps(fixture()), text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['boot'], 'old')


class PlaybookTests(unittest.TestCase):
    def test_serial_global_failure_and_drain_gates(self):
        play = yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())[0]
        self.assertEqual(play['serial'], 1)
        self.assertTrue(play['any_errors_fatal'])
        block = play['tasks'][-1]
        self.assertIn('ansible.builtin.fail', block['rescue'][0])
        tasks = block['block']
        self.assertIn('drain', tasks[0]['ansible.builtin.command']['argv'])
        self.assertNotIn('--force', tasks[0]['ansible.builtin.command']['argv'])
        self.assertNotIn('ignore_errors', tasks[0])
        self.assertEqual(tasks[1]['ansible.builtin.command']['argv'][1], '-Syu')
        self.assertIn('--ignore', tasks[1]['ansible.builtin.command']['argv'])
        self.assertFalse(play['vars']['maintenance_delete_emptydir_data'])
        self.assertTrue(any('maintenance_packages_before.stdout != maintenance_packages_after.stdout' in str(t) for t in tasks))
        self.assertTrue(any('maintenance_return_success | bool' in str(t) for t in tasks))

    def test_drain_failure_has_no_continuation_override(self):
        play = yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())[0]
        block = play['tasks'][-1]
        drain = block['block'][0]
        # Ansible command's nonzero drain result must fail into the terminal rescue.
        for override in ('ignore_errors', 'failed_when', 'until'):
            self.assertNotIn(override, drain)
        self.assertEqual(len(block['rescue']), 1)
        self.assertIn('ansible.builtin.fail', block['rescue'][0])
        self.assertNotIn('always', block)

    def test_pending_marker_and_package_delta_reboot_expression(self):
        from jinja2 import Environment
        from types import SimpleNamespace as N
        play = yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())[0]
        fact = next(t['ansible.builtin.set_fact'] for t in play['tasks'][-1]['block']
                    if 'maintenance_reboot' in t.get('ansible.builtin.set_fact', {}))
        expression = Environment().compile_expression(fact['maintenance_reboot'][2:-2])
        for changed, before, after, expected in [(False, False, False, False), (True, False, False, True), (False, True, False, True), (False, False, True, True)]:
            result = expression(maintenance_packages_before=N(stdout='old'), maintenance_packages_after=N(stdout='new' if changed else 'old'),
                                maintenance_markers=N(results=[None, None, N(stat=N(exists=before))]), maintenance_pending_after=N(stat=N(exists=after)))
            self.assertEqual(result, expected)

    def test_return_observations_stop_after_success_and_have_deadline(self):
        play = yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())[0]
        observation = next(t for t in play['tasks'][-1]['block'] if t.get('ansible.builtin.include_tasks') == 'tasks/maintenance-return.yml')
        self.assertEqual(observation['loop'], '{{ range(60) | list }}')
        gate = yaml.safe_load((ROOT / 'playbooks/tasks/maintenance-return.yml').read_text())[0]
        self.assertEqual(gate['when'], 'not maintenance_return_success | bool')
        self.assertIn('ansible.builtin.assert', play['tasks'][-1]['block'][play['tasks'][-1]['block'].index(observation) + 1])

    def test_debian_polling_orders_cache_before_upgrade(self):
        tasks = yaml.safe_load((ROOT / 'playbooks/update-upgrade.yml').read_text())[1]['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertTrue(tasks[0]['ansible.builtin.apt']['update_cache'])
        self.assertEqual(tasks[1]['ansible.builtin.apt']['upgrade'], 'dist')
        for task in tasks:
            self.assertGreater(task['poll'], 0)
            self.assertGreater(task['async'], task['poll'])
            self.assertNotIn('ignore_errors', task)
            self.assertNotIn('failed_when', task)

    def test_real_ansible_polling_propagates_cache_failure(self):
        # Exercise installed Ansible's polling function with no job/process/connection.
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock
        from ansible.executor import task_executor as executor
        @contextmanager
        def result_context(value=None):
            yield SimpleNamespace(**(value or {}))
        task = SimpleNamespace(check_mode=False, environment={}, delegate_to=None, async_val=300, poll=10)
        handler = Mock()
        handler.run.return_value = {'finished': True, 'failed': True, 'msg': 'cache failed'}
        worker = Mock()
        worker._task = task
        worker._shared_loader_obj.action_loader.get.return_value = handler
        with patch.object(executor.Task, 'load'), patch.object(executor.Templar, '_from_template_engine'), patch.object(executor.UnifiedTaskResult, 'create_and_record', side_effect=result_context), patch.object(executor.time, 'sleep'):
            result = executor.TaskExecutor._poll_async_result(worker, SimpleNamespace(async_job_id='fixture'), Mock(), {})
        self.assertTrue(result.failed)
        self.assertEqual(result.msg, 'cache failed')
        handler.cleanup.assert_called_with(force=True)


class AnsibleRuntimeTests(unittest.TestCase):
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

    def test_actual_ansible_loading_and_argv_templating(self):
        import shlex
        from ansible.parsing.dataloader import DataLoader
        from ansible.playbook import Playbook
        from ansible.template import Templar
        from ansible.vars.manager import VariableManager
        from ansible.plugins.loader import init_plugin_loader
        from ansible import constants
        init_plugin_loader()
        loader = DataLoader()
        source = str(ROOT / 'playbooks/kubernetes-nodes-update-v2.yml')
        with patch.object(constants, 'CACHE_PLUGIN', 'ansible.builtin.memory'):
            manager = VariableManager(loader=loader)
        Playbook.load(source, loader=loader, variable_manager=manager)
        play = loader.load_from_file(source, trusted_as_template=True)[0]
        variables = dict(play['vars']) | {'inventory_hostname': 'kube'}
        templar = Templar(loader=loader, variables=variables)
        self.assertEqual(templar.template(play['vars']['maintenance_node']), 'kube-controller')
        variables['maintenance_node'] = 'node ; $(false) "quoted"'
        variables['maintenance_kubectl_argv'] = ['doas', 'kubectl', '--kubeconfig=/path with spaces/$HOME']
        templar = Templar(loader=loader, variables=variables)
        snapshot = loader.load_from_file(str(ROOT / 'playbooks/tasks/maintenance-snapshot.yml'), trusted_as_template=True)
        tasks = snapshot + [t for t in play['tasks'][-1]['block']
                            if 'ansible.builtin.command' in t and 'timeout' in str(t['ansible.builtin.command'])]
        for task in tasks:
            command = task['ansible.builtin.command']
            argv = templar.template(command['argv'])
            self.assertEqual(argv[0], 'timeout')
            self.assertFalse(command['expand_argument_vars'])
            remote = shlex.split(argv[-1])
            self.assertEqual(remote[:3], variables['maintenance_kubectl_argv'])
            if remote[3] in ('drain', 'uncordon'):
                self.assertEqual(remote[4], variables['maintenance_node'])

    def run_fixture(self, observing):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            work = Path(directory)
            (work / 'files').mkdir()
            (work / 'files/maintenance_health.py').write_text((ROOT / 'playbooks/files/maintenance_health.py').read_text())
            returns = yaml.safe_load((ROOT / 'playbooks/tasks/maintenance-return.yml').read_text())
            returns[0]['block'][-1]['ansible.builtin.pause']['seconds'] = 0
            (work / 'maintenance-return.yml').write_text(yaml.safe_dump(returns))
            (work / 'maintenance-snapshot.yml').write_text((ROOT / 'playbooks/tasks/maintenance-snapshot.yml').read_text())
            data = fixture()
            data['node'] = 'kube-controller'
            for obj in data['resources']['items']:
                if obj['kind'] in ('Node', 'Lease'):
                    obj['metadata']['name'] = data['node']
                if obj['kind'] == 'Pod':
                    obj['spec']['nodeName'] = data['node']
            baseline = health.check(data)
            data['resources']['items'][0]['status']['nodeInfo']['bootID'] = 'new'
            (work / 'input.json').write_text(json.dumps(data))
            mock = work / 'mock_ssh.py'
            mock.write_text("""import json, pathlib, shlex, sys
work = pathlib.Path(__file__).parent
count = work / 'calls'
n = int(count.read_text()) if count.exists() else 0
count.write_text(str(n + 1))
argv = shlex.split(sys.argv[-1])
assert argv[:4] == ['doas', 'kubectl', '--kubeconfig=/fixture path/quote\";$(false)', '--request-timeout=10s'], argv
if n == 0:
    sys.exit('fixture: control plane unreachable')
data = json.loads((work / 'input.json').read_text())
print(json.dumps(data['resources'] if argv[4] == 'get' else data['version']))
""")
            play = yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())[0]
            variables = play['vars'] | {
                'ansible_connection': 'local', 'ansible_python_interpreter': PYTHON,
                'ansible_remote_tmp': str(work / 'remote'),
                'maintenance_ssh_argv': [PYTHON, '-B', str(mock)],
                'maintenance_kubectl_argv': ['doas', 'kubectl', '--kubeconfig=/fixture path/quote";$(false)', '--request-timeout=10s'],
                'maintenance_baseline': baseline, 'maintenance_return_success': False,
                'maintenance_reboot': True,
                'maintenance_after': (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=5)).isoformat()}
            tasks = [
                {'ansible.builtin.include_tasks': 'maintenance-return.yml', 'loop': '{{ range(4) | list }}',
                 'when': 'not maintenance_return_success | bool'},
                {'ansible.builtin.assert': {'that': ['maintenance_return_success | bool', "maintenance_node == 'kube-controller'", "maintenance_baseline.boot == 'old'"]}}]
            if not observing:
                tasks = [{'ansible.builtin.include_tasks': 'maintenance-snapshot.yml'}]
            (work / 'play.yml').write_text(yaml.safe_dump([{'hosts': 'kube', 'gather_facts': False, 'become': False, 'vars': variables, 'tasks': tasks}]))
            (work / 'ansible.cfg').write_text('[defaults]\nforks=1\nretry_files_enabled=False\n')
            env = os.environ | {'ANSIBLE_CONFIG': str(work / 'ansible.cfg'), 'ANSIBLE_LOCAL_TEMP': str(work / 'local')}
            result = subprocess.run([PYTHON, '-B', '-m', 'ansible.cli.playbook', '-i', 'kube,', str(work / 'play.yml')],
                                    cwd=work, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0 if observing else 2, result.stdout + result.stderr)
            self.assertEqual((work / 'calls').read_text(), '4' if observing else '1')

    def test_unreachable_then_return_and_skipped_includes(self):
        self.run_fixture(True)

    def test_initial_read_failure_is_fatal(self):
        self.run_fixture(False)


if __name__ == '__main__':
    unittest.main()
