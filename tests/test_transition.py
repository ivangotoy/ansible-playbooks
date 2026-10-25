"""Synthetic contract tests only; not Ansible or live upgrade evidence."""
import copy
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playbooks/files'))
from maintenance_transition import plan

PACKAGES = ('kubeadm', 'kubelet', 'kubectl')


def fixture(current='1.37.1', target='1.38.0'):
    return dict(api='v' + current, inventory=['worker', 'kube'],
                cluster_nodes=['worker', 'kube-controller'], nodes=[
                    dict(host=host, node=node, uid=host + '-uid', control_plane=host == 'kube',
                         schedulable=True, reported='v' + current, cluster_config='v' + current,
                         installed={p: current + '-1' for p in PACKAGES},
                         candidates={p: target + '-1' for p in PACKAGES})
                    for host, node in [('worker', 'worker'), ('kube', 'kube-controller')]])


class TransitionTests(unittest.TestCase):
    def test_supported_minor_controller_first(self):
        result = plan(fixture())
        self.assertEqual(result['target'], 'v1.38.0')
        self.assertTrue(result['upgrade_api'])
        self.assertEqual([n['host'] for n in result['nodes']], ['kube', 'worker'])
        self.assertTrue(all(n['reconcile'] for n in result['nodes']))

    def test_same_minor_patch(self):
        self.assertTrue(plan(fixture(target='1.37.2'))['upgrade_api'])

    def test_true_noop(self):
        result = plan(fixture(target='1.37.1'))
        self.assertFalse(result['upgrade_api'])
        self.assertTrue(all(not n['reconcile'] and not n['packages'] for n in result['nodes']))

    def test_api_at_target_keeps_unfinished_nodes(self):
        data = fixture()
        data['api'] = 'v1.38.0'
        result = plan(data)
        self.assertFalse(result['upgrade_api'])
        self.assertTrue(all(n['reconcile'] for n in result['nodes']))
        data['nodes'][1]['cluster_config'] = 'v1.38.0'
        data['nodes'][1]['reported'] = 'v1.38.0'
        data['nodes'][1]['installed'] = dict(data['nodes'][1]['candidates'])
        result = plan(data)
        self.assertFalse(result['nodes'][0]['reconcile'])
        self.assertTrue(result['nodes'][1]['reconcile'])

    def test_pending_same_upstream_rebuild_forces_restart(self):
        data = fixture(target='1.37.1')
        node = data['nodes'][0]
        node['installed']['kubelet'] = node['candidates']['kubelet'] = '1.37.1-2'
        node['pending'] = dict(target='v1.37.1', candidates=dict(node['candidates']),
                               node=node['node'], uid=node['uid'], apply_complete=True)
        data['nodes'][1]['installed']['kubelet'] = data['nodes'][1]['candidates']['kubelet'] = '1.37.1-2'
        result = plan(data)
        self.assertTrue(result['nodes'][1]['reconcile'])
        self.assertEqual(result['nodes'][1]['packages'], [])
        self.assertFalse(result['nodes'][0]['reconcile'])

    def test_pending_identity_and_target_mismatch_rejected(self):
        for field, value in [('target', 'v1.36.0'), ('uid', 'replaced'),
                             ('node', 'other'), ('candidates', {}), ('apply_complete', 'yes')]:
            data = fixture(target='1.37.1')
            node = data['nodes'][0]
            node['pending'] = dict(target='v1.37.1', candidates=dict(node['candidates']),
                                   node=node['node'], uid=node['uid'], apply_complete=True)
            node['pending'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Pending maintenance'):
                plan(data)

    def test_apply_pending_survives_api_and_package_equality(self):
        data = fixture(current='1.38.0', target='1.38.0')
        node = data['nodes'][1]
        node['pending'] = dict(target='v1.38.0', candidates=dict(node['candidates']),
                               node=node['node'], uid=node['uid'], apply_complete=False)
        self.assertTrue(plan(data)['nodes'][0]['apply'])
        node['pending']['apply_complete'] = True
        self.assertFalse(plan(data)['nodes'][0]['apply'])
        self.assertTrue(plan(data)['nodes'][0]['reconcile'])

    def test_unmarked_partial_apply_checks_cluster_configuration(self):
        data = fixture(current='1.38.0', target='1.38.0')
        data['nodes'][1]['cluster_config'] = 'v1.37.1'
        self.assertTrue(plan(data)['nodes'][0]['apply'])
        data['nodes'][1]['cluster_config'] = 'v1.38.0'
        self.assertFalse(plan(data)['nodes'][0]['apply'])
        del data['nodes'][1]['cluster_config']
        with self.assertRaises(KeyError):
            plan(data)

    def test_pending_restart(self):
        data = fixture()
        data['api'] = 'v1.38.0'
        for node in data['nodes']:
            node['installed'] = dict(node['candidates'])
        self.assertTrue(all(n['reconcile'] and not n['packages'] for n in plan(data)['nodes']))

    def test_kubeadm_already_installed(self):
        data = fixture()
        data['nodes'][1]['installed']['kubeadm'] = '1.38.0-1'
        result = plan(data)
        self.assertEqual(result['nodes'][0]['packages'], ['kubelet', 'kubectl'])
        self.assertTrue(result['upgrade_api'])

    def test_release_change_selected(self):
        data = fixture(target='1.37.1')
        for node in data['nodes']:
            node['candidates']['kubeadm'] = '1.37.1-2'
        result = plan(data)
        self.assertFalse(result['upgrade_api'])
        self.assertTrue(all(n['packages'] == ['kubeadm'] for n in result['nodes']))

    def test_invalid_paths(self):
        for current, target in [('1.37.1', '1.36.9'), ('1.37.1', '1.39.0'),
                                ('1.37.1', '2.0.0'), ('1.37.2', '1.37.1')]:
            with self.subTest(current=current, target=target), self.assertRaises(ValueError):
                plan(fixture(current, target))

    def test_release_downgrade(self):
        data = fixture(target='1.37.1')
        data['nodes'][0]['installed']['kubeadm'] = '1.37.1-2'
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            plan(data)

    def test_candidate_drift(self):
        for replacement in ['1.38.0-2', '1.38.1-1']:
            data = fixture()
            data['nodes'][0]['candidates']['kubectl'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                plan(data)

    def test_mismatched_package_target(self):
        data = fixture()
        for node in data['nodes']:
            node['candidates']['kubectl'] = '1.37.1-1'
        with self.assertRaises(ValueError):
            plan(data)

    def test_mapping_and_cordon(self):
        for field, value in [('uid', ''), ('schedulable', False), ('control_plane', True),
                             ('node', 'kube-controller'), ('host', 'kube')]:
            data = fixture()
            data['nodes'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan(data)
        for field, value in [('inventory', ['kube']), ('cluster_nodes', ['unmanaged'])]:
            data = fixture()
            data[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan(data)

    def test_reported_newer_than_installed(self):
        data = fixture()
        data['nodes'][0]['reported'] = 'v1.38.0'
        with self.assertRaises(ValueError):
            plan(data)

    def test_input_unchanged(self):
        data = fixture()
        before = copy.deepcopy(data)
        plan(data)
        self.assertEqual(before, data)


if __name__ == '__main__':
    unittest.main()
