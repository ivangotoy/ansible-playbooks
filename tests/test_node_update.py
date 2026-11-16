"""Offline tests of the decisions in group_vars/kubernetes.yml; values are made up."""
import json
from pathlib import Path
import unittest

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar

ROOT = Path(__file__).resolve().parents[1]
LOADER = DataLoader()
VARS = LOADER.load_from_file(str(ROOT / 'group_vars/kubernetes.yml'), trusted_as_template=True)
PLAYS = LOADER.load_from_file(str(ROOT / 'playbooks/kubernetes-nodes-update-v2.yml'), trusted_as_template=True)


def render(name, host='node-09', **facts):
    return Templar(loader=LOADER, variables=dict(VARS, inventory_hostname=host, **facts)).template(VARS[name])


def stat(exists):
    return {'stat': {'exists': exists}}


class RebootDecisionTests(unittest.TestCase):
    def decide(self, packages, marker=False, kernel=True):
        return render('node_update_reboot_required', node_update_download={'packages': packages},
                      node_update_marker_stat=stat(marker), node_update_kernel_stat=stat(kernel))

    def test_ordinary_packages_need_no_reboot(self):
        self.assertIs(self.decide(['bash', 'containerd', 'kubelet', 'linux-api-headers', 'systemd-ui']), False)

    def test_boot_critical_packages_need_a_reboot(self):
        for name in ('systemd', 'systemd-libs', 'glibc', 'intel-ucode', 'amd-ucode', 'linux-firmware-intel'):
            with self.subTest(name=name):
                self.assertIs(self.decide(['bash', name]), True)

    def test_hook_marker_needs_a_reboot(self):
        self.assertIs(self.decide([], marker=True), True)

    def test_replaced_running_kernel_needs_a_reboot(self):
        self.assertIs(self.decide(['bash'], kernel=False), True)


def tasks(items):
    for task in items:
        yield task
        yield from tasks(task.get('block', []))


def pacman_print(*targets):
    """Output of the repository version task for (name, version) targets, in its own --print-format."""
    task = next(task for play in PLAYS for task in tasks(play.get('tasks', []))
                if task.get('register') == 'node_update_kube_candidate')
    argv = task['ansible.builtin.command']['argv']
    form = argv[argv.index('--print-format') + 1]
    return '\n'.join(form.replace('%n', name).replace('%v', version) for name, version in targets) + '\n'


class KubernetesStepTests(unittest.TestCase):
    def step(self, host, candidate, api, kubelet='v1.50.1', dependencies=()):
        candidate = pacman_print(*dependencies, ('kubeadm', candidate))
        facts = dict(node_update_kube_candidate={'stdout': candidate},
                     node_update_api={'stdout': json.dumps({'serverVersion': {'gitVersion': api}})},
                     node_update_node_read={'stdout': json.dumps({'status': {'nodeInfo': {'kubeletVersion': kubelet}}})})
        return tuple(render(name, host, **facts) for name in
                     ('node_update_kube_step_supported', 'node_update_kubeadm_apply', 'node_update_kubeadm_node'))

    def test_control_plane_patch_and_next_minor_are_applied(self):
        self.assertEqual(self.step('kube', '1.50.2-1', 'v1.50.1'), (True, True, False))
        self.assertEqual(self.step('kube', '1.51.0-1', 'v1.50.1'), (True, True, False))

    def test_control_plane_rebuild_or_resume_at_target_skips_apply(self):
        self.assertEqual(self.step('kube', '1.50.1-2', 'v1.50.1'), (True, False, False))

    def test_control_plane_rejects_skipped_minor_downgrade_and_major(self):
        for candidate in ('1.52.0-1', '1.49.9-1', '2.0.0-1'):
            with self.subTest(candidate=candidate):
                self.assertIs(self.step('kube', candidate, 'v1.50.1')[0], False)

    def test_worker_follows_the_api_server_and_never_passes_it(self):
        self.assertEqual(self.step('node-09', '1.51.0-1', 'v1.51.0'), (True, False, True))
        self.assertEqual(self.step('node-09', '1.50.1-2', 'v1.50.1'), (True, False, False))
        self.assertIs(self.step('node-09', '1.51.0-1', 'v1.50.1')[0], False)


    def test_dependency_printed_with_kubeadm_does_not_set_the_target(self):
        dependency = [('conntrack-tools', '1.4.8-1')]
        self.assertEqual(self.step('kube', '1.51.0-1', 'v1.50.1', dependencies=dependency), (True, True, False))
        self.assertIs(self.step('node-09', '1.51.0-1', 'v1.50.1', dependencies=dependency)[0], False)


class StateTests(unittest.TestCase):
    def test_pending_lists_and_previous_cordon(self):
        facts = dict(node_update_download={'packages': ['bash', 'kubelet']}, node_update_aur={'stdout_lines': ['x 1 -> 2']})
        self.assertIs(render('node_update_kubernetes_pending', **facts), True)
        self.assertIs(render('node_update_kubernetes_pending', node_update_download={'msg': 'Nothing to upgrade'}), False)
        for spec, expected in (({}, False), ({'unschedulable': True}, True)):
            read = {'stdout': json.dumps({'spec': spec})}
            self.assertIs(render('node_update_was_cordoned', node_update_node_read=read), expected)

    def test_control_plane_maps_to_its_node_name(self):
        self.assertEqual(render('node_update_node_name', 'kube'), 'kube-controller')
        self.assertEqual(render('node_update_node_name'), 'node-09')

    def test_summary_of_an_upgraded_and_of_a_skipped_node(self):
        reported = {'stdout': 'v1.50.1'}
        self.assertEqual(render('node_update_summary', node_update_download={'packages': ['a', 'b']},
                                node_update_aur={'stdout_lines': ['x 1 -> 2']}, node_update_rebooted=True,
                                node_update_elapsed=97, node_update_reported=reported),
                         'node-09: 3 packages upgraded, rebooted yes, kubelet v1.50.1, 97 s')
        self.assertEqual(render('node_update_summary', node_update_download={}, node_update_aur={'stdout_lines': []},
                                node_update_reported=reported),
                         'node-09: 0 packages upgraded, rebooted no, kubelet v1.50.1, 0 s')


class ResumeTests(unittest.TestCase):
    """A run that died after the drain must be finished by the next run."""

    def select(self, packages=(), aur=(), marker=False, kernel=True, annotations=None):
        metadata = {} if annotations is None else {'annotations': annotations}
        return render('node_update_selected', node_update_download={'packages': list(packages)},
                      node_update_aur={'stdout_lines': list(aur)},
                      node_update_marker_stat=stat(marker), node_update_kernel_stat=stat(kernel),
                      node_update_node_check={'stdout': json.dumps({'metadata': metadata})})

    def test_idle_node_is_left_alone(self):
        self.assertIs(self.select(annotations={'other': 'x'}), False)

    def test_pending_packages_select_the_node(self):
        self.assertIs(self.select(packages=['bash']), True)
        self.assertIs(self.select(aur=['x 1 -> 2']), True)

    def test_outstanding_reboot_selects_the_node(self):
        self.assertIs(self.select(marker=True), True)
        self.assertIs(self.select(kernel=False), True)

    def test_cordon_left_by_an_interrupted_run_selects_the_node(self):
        self.assertIs(self.select(annotations={VARS['node_update_cordon_annotation']: '1700000000'}), True)

    def test_resumed_node_is_rebooted_without_pending_packages(self):
        self.assertIs(render('node_update_reboot_required', node_update_resume=True, node_update_download={},
                             node_update_marker_stat=stat(False), node_update_kernel_stat=stat(True)), True)

    def test_cordon_of_an_interrupted_run_is_lifted_but_an_operator_cordon_is_kept(self):
        mark = VARS['node_update_cordon_annotation']
        for annotations, expected in (({mark: '1700000000'}, False), ({}, True)):
            read = {'stdout': json.dumps({'spec': {'unschedulable': True}, 'metadata': {'annotations': annotations}})}
            with self.subTest(annotations=annotations):
                self.assertIs(render('node_update_was_cordoned', node_update_node_read=read), expected)


class PlaybookTests(unittest.TestCase):
    def test_nodes_are_upgraded_one_at_a_time_control_plane_first_and_stop_on_failure(self):
        play = PLAYS[1]
        self.assertEqual(play['hosts'], VARS['node_update_control_plane'] + ':kubernetes:&node_update_pending')
        self.assertEqual(play['serial'], 1)
        self.assertIs(play['any_errors_fatal'], True)

    def test_selection_and_upgrade_cover_resumed_nodes(self):
        tasks = {task['name']: task for task in PLAYS[0]['tasks']}
        self.assertEqual(tasks['Select the nodes that have upgrades or need to be resumed']['when'],
                         'node_update_selected')
        block = PLAYS[1]['tasks'][1]
        self.assertIn('node_update_resume', block['when'])

    def test_every_kubectl_call_is_bounded_in_time(self):
        self.assertIn('--request-timeout=10s', render('node_update_kubectl_request'))
        calls = [task['ansible.builtin.command']['argv'] for play in PLAYS for task in tasks(play['tasks'])
                 if 'node_update_kubectl' in str(task.get('ansible.builtin.command', {}).get('argv', ''))]
        self.assertEqual(len(calls), 13)
        for argv in calls:
            with self.subTest(argv=argv):
                self.assertTrue('node_update_kubectl_request +' in argv or "'--timeout=' ~" in argv)

    def test_cordon_mark_is_set_before_the_drain_and_removed_after_the_uncordon(self):
        names = [task['name'] for task in PLAYS[1]['tasks'][1]['block']]
        order = [names.index(name) for name in ('Mark the node as cordoned by this run', 'Drain the node',
                                                'Uncordon the node', 'Remove the cordon mark of this run')]
        self.assertEqual(order, sorted(order))


class UpgradePlanTests(unittest.TestCase):
    """Tasks of the upgrade play that run per scenario; covers paths not yet run on a live node."""

    def plan(self, host, packages=(), aur=(), resume=False, unschedulable=False, annotations=None,
             candidate='1.51.0-1', api='v1.50.1', kubelet='v1.50.1'):
        node = {'spec': {'unschedulable': unschedulable}, 'metadata': {'annotations': annotations or {}},
                'status': {'nodeInfo': {'kubeletVersion': kubelet}}}
        facts = dict(VARS, inventory_hostname=host, node_update_started='1700000000',
                     node_update_download={'packages': list(packages)}, node_update_aur={'stdout_lines': list(aur)},
                     node_update_resume=resume, node_update_marker_stat=stat(False),
                     node_update_kernel_stat=stat(True), node_update_node_read={'stdout': json.dumps(node)},
                     node_update_kube_candidate={'stdout': pacman_print(('kubeadm', candidate))},
                     node_update_api={'stdout': json.dumps({'serverVersion': {'gitVersion': api}})},
                     node_update_etcd={'stdout_lines': ['0123abcd']})
        templar = Templar(loader=LOADER, variables=facts)
        names = []

        def walk(items):
            for task in items:
                when = task.get('when', [])
                if not all(templar.evaluate_conditional(c) for c in ([when] if isinstance(when, str) else when)):
                    continue
                if 'block' in task:
                    walk(task['block'])
                    continue
                names.append(task['name'])
                if 'ansible.builtin.assert' in task:
                    self.assertTrue(templar.evaluate_conditional(task['ansible.builtin.assert']['that']))
                command = task.get('ansible.builtin.command', {})
                if 'argv' in command and 'loop' not in task:
                    self.assertTrue(all(isinstance(arg, str) for arg in templar.template(command['argv'])))

        walk(PLAYS[1]['tasks'])
        return names

    def assertOrdered(self, names, expected):
        self.assertEqual([name for name in names if name in expected], expected)

    def test_control_plane_minor_step_backs_up_then_applies_before_the_install(self):
        names = self.plan('kube', packages=['kubeadm', 'kubelet', 'bash'])
        self.assertOrdered(names, ['Mark the node as cordoned by this run', 'Drain the node', 'Archive /etc/kubernetes',
                                   'Save an etcd snapshot', 'Move the etcd snapshot into the backup directory',
                                   'Install the new kubeadm', 'Upgrade the control plane',
                                   'Install the downloaded upgrade',
                                   'Restart the kubelet after the Kubernetes packages changed',
                                   'Uncordon the node', 'Remove the cordon mark of this run'])
        self.assertNotIn('Upgrade the kubelet configuration of the worker', names)
        self.assertNotIn('Reboot the node', names)
        self.assertNotIn('Build and install pending AUR upgrades', names)

    def test_worker_step_runs_kubeadm_upgrade_node_without_control_plane_backup(self):
        names = self.plan('node-09', packages=['kubeadm', 'kubelet'], api='v1.51.0')
        self.assertOrdered(names, ['Drain the node', 'Install the new kubeadm',
                                   'Upgrade the kubelet configuration of the worker',
                                   'Install the downloaded upgrade', 'Uncordon the node'])
        for name in ('Archive /etc/kubernetes', 'Save an etcd snapshot', 'Upgrade the control plane'):
            self.assertNotIn(name, names)

    def test_ordinary_upgrade_skips_kubeadm(self):
        names = self.plan('node-09', packages=['bash'])
        self.assertIn('Install the downloaded upgrade', names)
        self.assertNotIn('Install the new kubeadm', names)
        self.assertNotIn('Read the Kubernetes version offered by the repository', names)

    def test_aur_only_node_is_neither_drained_nor_rebooted(self):
        self.assertEqual(self.plan('node-09', aur=['x 1 -> 2']),
                         ['Record the start time', 'Build and install pending AUR upgrades', 'Record the result'])

    def test_resumed_node_is_drained_rebooted_and_uncordoned(self):
        names = self.plan('node-09', resume=True, unschedulable=True,
                          annotations={VARS['node_update_cordon_annotation']: '1690000000'})
        self.assertOrdered(names, ['Mark the node as cordoned by this run', 'Drain the node', 'Reboot the node',
                                   'Uncordon the node', 'Remove the cordon mark of this run'])
        self.assertNotIn('Install the new kubeadm', names)

    def test_operator_cordon_is_drained_and_kept(self):
        names = self.plan('node-09', packages=['bash'], unschedulable=True)
        self.assertIn('Drain the node', names)
        for name in ('Mark the node as cordoned by this run', 'Uncordon the node', 'Remove the cordon mark of this run'):
            self.assertNotIn(name, names)
