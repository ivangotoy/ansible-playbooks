"""Execute the production Ansible flow against local, stateful command endpoints.

The helper tests exercise archive operations separately. These fixtures mock host
commands/services, not Ansible includes, conditionals, health or transition gates.
Run AnsibleFlowTests on the trusted controller (Unix sockets are required).
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml

from test_maintenance import ROOT, PYTHON, fixture, load


class FlowGateTests(unittest.TestCase):
    def test_fixture_commands_are_local_endpoints(self):
        def inspect(argv, cwd=None, **kwargs):
            work=Path(cwd)
            documents=[yaml.safe_load((work/'play.yml').read_text())]
            for play in documents[0]:
                self.assertIs(play['become'],False)
            documents.extend(yaml.safe_load(p.read_text()) for p in (work/'tasks').glob('*.yml'))
            def walk(value):
                if isinstance(value,list):
                    for item in value: walk(item)
                if not isinstance(value,dict): return
                if 'ansible.builtin.include_tasks' in value:
                    self.assertNotIn('become',value)
                    self.assertNotIn('become_user',value)
                if 'ansible.builtin.command' in value:
                    command=value['ansible.builtin.command']['argv']
                    if isinstance(command,list):
                        self.assertIn(command[0],[PYTHON,'{{ ansible_playbook_python }}','date'])
                    elif command=='{{ item }}':
                        self.assertTrue(all(item[0]==PYTHON for item in value['loop']))
                    else:
                        self.assertIn('maintenance_ssh_argv',command)
                for key in ('tasks','block','rescue','always'): walk(value.get(key,[]))
            for document in documents: walk(document)
            return subprocess.CompletedProcess(argv,0,'fixture generation only','')
        with patch.object(subprocess,'run',side_effect=inspect):
            AnsibleFlowTests().run_flow()

    def test_actual_ansible_preparation_input_reaches_transition_planner(self):
        from ansible.parsing.dataloader import DataLoader
        from ansible.template import Templar
        from ansible.plugins.loader import init_plugin_loader
        init_plugin_loader()
        loader=DataLoader()
        play=loader.load_from_file(str(ROOT/'playbooks/kubernetes-nodes-update-v2.yml'),trusted_as_template=True)[0]
        data=fixture()
        data['resources']['items'][0]['metadata'].update(uid='node-uid',labels={'node-role.kubernetes.io/control-plane':''})
        versions={p:'1.37.1-1' for p in ('kubeadm','kubelet','kubectl')}
        variables=dict(play['vars']) | dict(inventory_hostname='kube',maintenance_node='node1',
            maintenance_resources={'stdout':json.dumps(data['resources'])},maintenance_api={'stdout':json.dumps(data['version'])},
            maintenance_baseline={'version':'v1.37.1'},maintenance_protected_before={'stdout_lines':[p+' '+v for p,v in versions.items()]},
            maintenance_manifest={'candidates':{p:'1.38.0-1' for p in versions}},groups={'kubernetes':['kube']},
            maintenance_pending_stat={'stat':{'exists':False}},
            maintenance_cluster_config={'stdout':json.dumps({'data':{'ClusterConfiguration':'kubernetesVersion: v1.37.1'}})})
        task=next(t for t in play['tasks'] if t['name']=='Retain mapped node input for cluster-wide transition gate')
        variables.update(task['vars'])
        entry=Templar(loader=loader,variables=variables).template(task['ansible.builtin.set_fact']['maintenance_transition_input'])
        self.assertIs(entry['control_plane'],True)
        self.assertIs(entry['schedulable'],True)
        variables['hostvars']={'kube':{'maintenance_transition_input':entry}}
        command=next(t for t in play['tasks'] if t['name']=='Validate complete cluster target and transition before any drain')['ansible.builtin.command']
        payload=Templar(loader=loader,variables=variables).template(command['stdin'])
        self.assertEqual(load('maintenance_transition').plan(json.loads(payload))['target'],'v1.38.0')

    def test_resume_health_allows_one_minor_skew_only_in_transition(self):
        health = load('maintenance_health')
        data = fixture()
        data['transition'] = True
        data['version']['serverVersion']['gitVersion'] = 'v1.38.0'
        health.check(data)
        data['version']['serverVersion']['gitVersion'] = 'v1.39.0'
        with self.assertRaisesRegex(ValueError, 'skew'):
            health.check(data)

    def test_return_requires_target_and_same_node_uid(self):
        health = load('maintenance_health')
        data = fixture()
        data['resources']['items'][0]['metadata']['uid'] = 'original'
        data.update(baseline={'version': 'v1.36.0', 'target': 'v1.37.1', 'uid': 'original', 'boot': 'old'},
                    after='2000-01-01T00:00:00Z', reboot=False)
        health.check(data)
        data['baseline']['uid'] = 'replacement'
        with self.assertRaisesRegex(ValueError, 'UID'):
            health.check(data)


ENDPOINT = r'''
import base64, datetime, json, pathlib, shlex, sys
root = pathlib.Path(__file__).parent
statefile = root / 'state.json'
s = json.loads(statefile.read_text())
host, kind, *argv = sys.argv[1:]
def save(): statefile.write_text(json.dumps(s))
def event(label):
    s['events'].append(host + ':' + label)
    save()
def output(value): print(json.dumps(value))
def manifest():
    old = s['initial'][host]
    return dict(installed=old, candidates={p:s['target']+s['release'] for p in ('kubeadm','kubelet','kubectl')},
                archives=[dict(name=p,version=s['target']+s['release']) for p in old if old[p] != s['target']+s['release']])
if kind == 'script':
    action = shlex.split(argv[0])[1]
    event(action)
    if action in ('repo','aur','seal','verify'):
        if s.get('fail') == 'prepare' and action == 'seal': sys.exit('fixture archive verification failure')
        output(manifest())
    else:
        if s.get('fail') == host and action == 'rest': sys.exit('fixture transaction failure')
        if action == 'kubeadm': s['packages'][host]['kubeadm'] = s['target']+s['release']
        if action == 'rest':
            assert s['api'] == 'v'+s['target'], 'premature kubelet installation'
            s['packages'][host] = {p:s['target']+s['release'] for p in s['packages'][host]}
        save()
        output(dict(changed=True))
elif kind == 'restart':
    if s.get('fail') == 'restart-after-packages' and host == 'node-01':
        sys.exit('fixture interruption before restart')
    assert s['api'] == 'v'+s['target'], 'premature kubelet restart'
    event('restart')
    s['reported'][host] = 'v'+s['target']
    save()
elif kind == 'reboot':
    event('reboot')
    s['boots'][host] = 'new'
    (root / host / 'boot').write_text('new')
    save()
elif kind == 'ssh':
    argv = shlex.split(argv[-1])
    assert argv[:2] == ['doas','kubectl']
    argv = argv[4:]
    if argv[0] == 'version': output(dict(serverVersion=dict(gitVersion=s['api'])))
    elif argv[0] == 'get' and argv[1] == '--raw=/readyz': print('ok')
    elif argv[:3] == ['get','configmap','kubeadm-config']:
        output({'data':{'ClusterConfiguration':'kubernetesVersion: '+s['cluster_config']}})
    elif argv[0] == 'get':
        data = json.loads((root / 'resources.json').read_text())
        for obj in data['items']:
            name = obj['metadata']['name']
            if obj['kind'] == 'Node':
                h = 'kube' if name == 'kube-controller' else name
                obj['status']['nodeInfo'].update(kubeletVersion=s['reported'][h],bootID=s['boots'][h])
                obj['spec']['unschedulable'] = s['cordon'].get(name,False)
            if obj['kind'] == 'Lease': obj['spec']['renewTime'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        output(data)
    elif argv[0] in ('drain','uncordon'):
        host = 'kube' if argv[1] == 'kube-controller' else argv[1]
        event('dry-drain' if '--dry-run=server' in argv else argv[0])
        if '--dry-run=server' not in argv:
            s['cordon'][argv[1]] = argv[0] == 'drain'
            save()
    else: raise AssertionError(argv)
elif kind == 'command':
    if argv[0] == 'timeout': argv = argv[2:]
    if argv[0] == 'pacman':
        names = argv[2:] or list(s['packages'][host])
        print('\n'.join(p+' '+s['packages'][host][p] for p in names))
    elif argv[:2] == ['kubeadm','version']: print('v'+s['packages'][host]['kubeadm'].split('-')[0])
    elif argv[0] == 'kubelet': print('Kubernetes v'+s['packages'][host]['kubelet'].split('-')[0])
    elif argv[0] == 'kubectl': output(dict(clientVersion=dict(gitVersion='v'+s['packages'][host]['kubectl'].split('-')[0])))
    elif argv[:3] == ['kubeadm','upgrade','apply']:
        event('apply')
        s['api'] = 'v'+s['target']
        save()
        if s.get('fail') == 'apply-after-api': sys.exit('fixture interruption before upload-config')
        s['cluster_config'] = s['api']
        save()
    elif argv[:3] == ['kubeadm','upgrade','node']: event('upgrade-node')
    elif argv[0] == 'systemctl': print('active')
    elif argv[0] == 'tar': event('backup-config')
    elif argv[0] == 'crictl':
        if 'ps' in argv: print('etcd-container')
        elif 'etcdutl' in argv: event('validate-snapshot')
        elif 'etcdctl' in argv: event('snapshot')
        else: raise AssertionError(argv)
    elif argv[0] == 'mv': event('retain-snapshot')
    elif argv[0] == 'sha256sum': print('fixture-checksum  fixture-backup')
    else: raise AssertionError(argv)
else: raise AssertionError((host,kind,argv))
'''


class AnsibleFlowTests(unittest.TestCase):
    maxDiff = None

    def run_flow(self, target='1.38.0', api='v1.37.1', fail=None, reboot=False, interrupt=None, release='-1', pending=None):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / 'files').mkdir()
            (work / 'tasks').mkdir()
            for name in ('maintenance_health.py', 'maintenance_transition.py'):
                shutil.copyfile(ROOT / 'playbooks/files' / name, work / 'files' / name)
            script = work / 'endpoint.py'
            script.write_text(ENDPOINT)
            original = fixture()['resources']['items']
            resources = []
            for host, name in [('kube','kube-controller'),('node-01','node-01'),('node-02','node-02')]:
                for obj in copy.deepcopy(original[:3]):
                    if obj['kind'] in ('Node','Lease'): obj['metadata']['name'] = name
                    if obj['kind'] == 'Node':
                        obj['metadata'].update(uid=host+'-uid',labels={'node-role.kubernetes.io/control-plane':''} if host == 'kube' else {})
                    if obj['kind'] == 'Pod':
                        obj['metadata']['name'] = 'kured-'+host
                        obj['spec']['nodeName'] = name
                    resources.append(obj)
                (work / host).mkdir()
                (work / host / 'boot').write_text('old')
                (work / host / 'cleanup').write_text('r! /var/lib/kured/reboot-required - - - -')
                if reboot: (work / host / 'os-reboot').touch()
                (work / host / 'etcd.yaml').write_text(yaml.safe_dump({'spec':{'containers':[{'volumeMounts':[{'name':'etcd-data','mountPath':'/var/lib/etcd'}]}], 'volumes':[{'name':'etcd-data','hostPath':{'path':'/var/lib/etcd'}}]}}))
            ds = copy.deepcopy(original[3])
            for key in ('desiredNumberScheduled','numberReady','updatedNumberScheduled','numberAvailable'): ds['status'][key] = 3
            resources.append(ds)
            (work / 'resources.json').write_text(json.dumps({'items':resources}))
            hosts = ['kube','node-01','node-02']
            packages = {h:{p:'1.37.1-1' for p in ('kubeadm','kubelet','kubectl')} for h in hosts}
            state = dict(target=target,release=release,api=api,cluster_config='v1.37.1',initial=copy.deepcopy(packages),packages=packages,
                         reported={h:'v1.37.1' for h in hosts},boots={h:'old' for h in hosts},cordon={},events=[],fail=interrupt or fail)
            if pending:
                (work / 'node-01/pending.json').write_text(json.dumps(pending))
            (work / 'state.json').write_text(json.dumps(state))
            prefix = [PYTHON,str(script),'{{ inventory_hostname }}']
            def transform(tasks):
                for task in tasks:
                    if 'vars' in task and 'ansible_remote_tmp' in task['vars']:
                        task['vars']['ansible_remote_tmp'] = str(work / 'remote')
                    task.pop('become_user',None)
                    if 'ansible.builtin.include_tasks' not in task:
                        task['become'] = False
                    for key in ('block','rescue','always'):
                        transform(task.get(key,[]))
                    if 'ansible.builtin.script' in task:
                        command = task.pop('ansible.builtin.script')['cmd']
                        task['ansible.builtin.command'] = {'argv':prefix+['script',command]}
                    elif 'ansible.builtin.command' in task:
                        command = task['ansible.builtin.command']
                        argv = command['argv']
                        # Remote kubectl uses the stateful SSH endpoint. Health,
                        # planner and clock remain real commands.
                        if isinstance(argv,list) and argv[0] not in ('{{ ansible_playbook_python }}','date'):
                            command['argv'] = prefix+['command']+argv
                        elif argv == '{{ item }}':
                            task['loop'] = [prefix+['command']+item for item in task['loop']]
                    if 'ansible.builtin.systemd_service' in task:
                        task.pop('ansible.builtin.systemd_service')
                        task['ansible.builtin.command'] = {'argv':prefix+['restart']}
                    if 'ansible.builtin.tempfile' in task:
                        task['ansible.builtin.tempfile']['path'] = str(work)+'/{{ inventory_hostname }}'
                    if 'ansible.builtin.file' in task:
                        task['ansible.builtin.file'].pop('owner',None)
                        if task['ansible.builtin.file']['path'] == '/var/lib/ansible-maintenance/pending.json':
                            task['ansible.builtin.file']['path'] = str(work)+'/{{ inventory_hostname }}/pending.json'
                    if 'ansible.builtin.slurp' in task:
                        path = task['ansible.builtin.slurp']['src']
                        leaf = {'/proc/sys/kernel/random/boot_id':'boot','/etc/tmpfiles.d/kured-maintenance.conf':'cleanup','/etc/kubernetes/manifests/etcd.yaml':'etcd.yaml','/var/lib/ansible-maintenance/pending.json':'pending.json'}[path]
                        task['ansible.builtin.slurp']['src'] = str(work)+'/{{ inventory_hostname }}/'+leaf
                    if 'ansible.builtin.stat' in task:
                        mapping = {'/var/lib/pacman/db.lck':'lock','/var/lib/kured/reboot-required':'sentinel','/var/run/reboot-required':'os-reboot','/var/lib/ansible-maintenance/pending.json':'pending.json'}
                        if 'loop' in task:
                            task['loop'] = [str(work)+'/{{ inventory_hostname }}/'+mapping[x] for x in task['loop']]
                        else:
                            path=task['ansible.builtin.stat']['path']
                            task['ansible.builtin.stat']['path']=str(work)+'/{{ inventory_hostname }}/'+mapping[path]
                    if 'ansible.builtin.copy' in task:
                        module=task['ansible.builtin.copy']
                        if module['dest']=='/var/lib/kured/reboot-required':
                            task.pop('ansible.builtin.copy')
                            task['ansible.builtin.command']={'argv':prefix+['reboot']}
                        elif module['dest'] in ('/var/lib/ansible-maintenance/completed.json','/var/lib/ansible-maintenance/pending.json'):
                            module['dest']=str(work)+'/{{ inventory_hostname }}/'+Path(module['dest']).name
                    if 'ansible.builtin.pause' in task: task['ansible.builtin.pause']['seconds']=0
                    if 'retries' in task: task['retries']=2; task['delay']=0
            for source in (ROOT / 'playbooks/tasks').glob('maintenance-*.yml'):
                tasks=yaml.safe_load(source.read_text())
                transform(tasks)
                (work / 'tasks' / source.name).write_text(yaml.safe_dump(tasks,sort_keys=False))
            plays=yaml.safe_load((ROOT / 'playbooks/kubernetes-nodes-update-v2.yml').read_text())
            for play in plays:
                play['become']=False
                play['vars'].update(ansible_connection='local',ansible_python_interpreter=PYTHON,
                                    ansible_remote_tmp=str(work/'remote'),maintenance_ssh_argv=[PYTHON,str(script),'remote','ssh'])
                transform(play['tasks'])
            (work/'play.yml').write_text(yaml.safe_dump(plays,sort_keys=False))
            (work/'inventory').write_text('[kubernetes]\nnode-01\nnode-02\nkube\n')
            (work/'ansible.cfg').write_text('[defaults]\nforks=1\nretry_files_enabled=False\nfact_caching=memory\n')
            env=os.environ | {'ANSIBLE_CONFIG':str(work/'ansible.cfg'),'ANSIBLE_LOCAL_TEMP':str(work/'local')}
            result=subprocess.run([PYTHON,'-B','-m','ansible.cli.playbook','-i',str(work/'inventory'),str(work/'play.yml')],
                                  cwd=work,env=env,text=True,capture_output=True,timeout=240)
            final=json.loads((work/'state.json').read_text())
            first = None
            if interrupt:
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                interrupted_host = 'kube' if interrupt == 'apply-after-api' else 'node-01'
                marker = json.loads((work/interrupted_host/'pending.json').read_text())
                self.assertEqual(marker['uid'],interrupted_host+'-uid')
                self.assertEqual(marker['apply_complete'],interrupt != 'apply-after-api')
                first = copy.deepcopy(final)
                final.update(initial=copy.deepcopy(final['packages']),cordon={},events=[],fail=None)
                (work/'state.json').write_text(json.dumps(final))
                result=subprocess.run([PYTHON,'-B','-m','ansible.cli.playbook','-i',str(work/'inventory'),str(work/'play.yml')],
                                      cwd=work,env=env,text=True,capture_output=True,timeout=240)
                final=json.loads((work/'state.json').read_text())
                final['interrupted'] = first
            output=result.stdout+result.stderr
            if fail: self.assertNotEqual(result.returncode,0,output)
            else: self.assertEqual(result.returncode,0,output)
            final['completed']=[h for h in hosts if (work/h/'completed.json').exists()]
            final['pending']=[h for h in hosts if (work/h/'pending.json').exists()]
            return final,output

    def test_minor_transition_orders_backup_apply_and_sequential_workers(self):
        state,_=self.run_flow()
        events=state['events']
        required=['kube:seal','node-01:seal','node-02:seal','kube:dry-drain','node-01:dry-drain','node-02:dry-drain']
        self.assertTrue(all(events.index(e)<events.index('kube:drain') for e in required))
        order=['kube:drain','kube:backup-config','kube:snapshot','kube:validate-snapshot','kube:kubeadm','kube:apply','kube:rest','kube:restart','kube:uncordon','node-01:drain','node-01:kubeadm','node-01:upgrade-node','node-01:rest','node-01:restart','node-01:uncordon','node-02:drain']
        self.assertEqual(sorted(order,key=events.index),order)
        self.assertEqual(state['completed'],['kube','node-01','node-02'])
        self.assertFalse(any(e.endswith(':reboot') for e in events))

    def test_same_minor_patch(self):
        state,_=self.run_flow(target='1.37.2')
        self.assertIn('kube:apply',state['events'])
        self.assertEqual(len(state['completed']),3)

    def test_api_at_target_resumes_node_local_work(self):
        state,_=self.run_flow(api='v1.38.0')
        self.assertIn('kube:apply',state['events'])
        self.assertNotIn('kube:upgrade-node',state['events'])
        self.assertIn('node-01:upgrade-node',state['events'])
        self.assertEqual(len(state['completed']),3)

    def test_interrupted_apply_after_api_move_reapplies_before_node_work(self):
        state,_=self.run_flow(interrupt='apply-after-api')
        self.assertEqual(state['interrupted']['api'],'v1.38.0')
        self.assertEqual(state['interrupted']['cluster_config'],'v1.37.1')
        self.assertIn('kube:apply',state['events'])
        self.assertNotIn('kube:upgrade-node',state['events'])
        self.assertEqual(state['pending'],[])
        self.assertEqual(len(state['completed']),3)

    def test_interrupted_same_upstream_rebuild_restarts_on_resume(self):
        state,_=self.run_flow(target='1.37.1',interrupt='restart-after-packages',release='-2')
        self.assertEqual(state['interrupted']['packages']['node-01']['kubelet'],'1.37.1-2')
        self.assertIn('node-01:drain',state['events'])
        self.assertIn('node-01:restart',state['events'])
        self.assertNotIn('kube:drain',state['events'])
        self.assertEqual(state['pending'],[])
        self.assertEqual(len(state['completed']),3)

    def test_stale_pending_identity_rejected_before_drain(self):
        marker=dict(target='v1.38.0',candidates={p:'1.38.0-1' for p in ('kubeadm','kubelet','kubectl')},
                    node='node-01',uid='replaced-uid',apply_complete=True)
        state,_=self.run_flow(pending=marker,fail='pending')
        self.assertFalse(any(e.endswith(':drain') for e in state['events']))
        self.assertEqual(state['pending'],['node-01'])

    def test_failure_stops_next_node(self):
        state,_=self.run_flow(fail='node-01')
        self.assertIn('node-01:rest',state['events'])
        self.assertNotIn('node-02:drain',state['events'])
        self.assertEqual(state['completed'],['kube'])

    def test_bad_preparation_never_drains(self):
        state,_=self.run_flow(fail='prepare')
        self.assertFalse(any(e.endswith(':drain') for e in state['events']))
        self.assertEqual(state['completed'],[])

    def test_noop_has_no_drain_reboot_or_kubeadm_mutation(self):
        state,_=self.run_flow(target='1.37.1')
        self.assertFalse(any(e.split(':')[1] in ('drain','reboot','kubeadm','rest','apply','upgrade-node') for e in state['events']))

    def test_real_os_marker_owns_reboot_even_with_no_package_changes(self):
        state,_=self.run_flow(target='1.37.1',reboot=True)
        self.assertEqual([e for e in state['events'] if e.endswith(':reboot')],['kube:reboot','node-01:reboot','node-02:reboot'])
        self.assertEqual(len(state['completed']),3)


if __name__=='__main__':
    unittest.main()
