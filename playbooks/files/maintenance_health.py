#!/usr/bin/env python3
"""Offline-testable Kubernetes maintenance gates; Python >= 3.11."""
import datetime
import json
import re
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))


def check(data):
    objects = data['resources']['items']
    nodes = [x for x in objects if x['kind'] == 'Node']
    node = next(x for x in nodes if x['metadata']['name'] == data['node'])
    for item in nodes:
        require(any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in item['status'].get('conditions', [])), 'A cluster node is not Ready')
    require(not node.get('spec', {}).get('unschedulable', False),
            'Cordoned node unsupported: resolve cordon ownership before maintenance')
    version = node['status']['nodeInfo']['kubeletVersion']
    server = data['version']['serverVersion']['gitVersion']
    minor = lambda v: re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?', v)
    require(minor(version) and minor(server), 'Unrecognized Kubernetes version')
    require(minor(version).groups()[:2] == minor(server).groups()[:2],
            'Maintenance requires kubelet and API server on the same minor version')
    pods = [x for x in objects if x['kind'] == 'Pod']
    for pod in pods:
        status = pod.get('status', {})
        if status.get('phase') == 'Succeeded':
            continue
        require(not pod['metadata'].get('deletionTimestamp') and
                status.get('phase') == 'Running' and
                any(c['type'] == 'Ready' and c['status'] == 'True'
                    for c in status.get('conditions', [])),
                'Workload unhealthy: ' + pod['metadata']['name'])
    for item in objects:
        kind = item['kind']
        status = item.get('status', {})
        spec = item.get('spec', {})
        if kind in ('Deployment', 'StatefulSet', 'DaemonSet'):
            require(status.get('observedGeneration', 0) >= item['metadata'].get('generation', 1),
                    'Workload controller has not observed its generation')
            desired = status.get('desiredNumberScheduled', 0) if kind == 'DaemonSet' else spec.get('replicas', 1)
            ready = status.get('numberReady', 0) if kind == 'DaemonSet' else status.get('readyReplicas', 0)
            require(ready == desired, 'Workload replicas are not healthy')
    ds = next(x for x in objects if x['kind'] == 'DaemonSet' and
              x['metadata']['namespace'] == 'kured' and x['metadata']['name'] == 'kured')
    spec = ds['spec']['template']['spec']
    require(len(spec['containers']) == 1 and not spec.get('initContainers'), 'Unsupported kured sidecars')
    container = spec['containers'][0]
    require(spec.get('hostPID') is True and container.get('securityContext', {}).get('privileged') is True and
            container.get('securityContext', {}).get('readOnlyRootFilesystem') is True,
            'Kured requires hostPID and a privileged container for its default reboot mechanism')
    require(container.get('command') in (['/usr/bin/kured'], ['/kured']), 'Explicit kured command required')
    args = container.get('args', [])
    allowed = {'--reboot-sentinel=/sentinel/reboot-required', '--period=1m',
               '--ds-name=kured', '--ds-namespace=kured', '--metrics-port=8080',
               '--drain-timeout=10m', '--reboot-command=/bin/systemctl reboot',
               '--time-zone=Europe/Sofia', '--annotate-nodes=true',
               '--prefer-no-schedule-taint=weave.works/kured-node-reboot',
               '--log-format=text', '--concurrency=1', '--force-reboot=false'}
    required = {'--reboot-sentinel=/sentinel/reboot-required', '--period=1m',
                '--ds-name=kured', '--ds-namespace=kured'}
    require(set(args) <= allowed and len(args) == len(set(args)) and required <= set(args),
            'Unsupported kured flags: require dedicated disk sentinel and safe drain/lock/schedule')
    env = container.get('env', [])
    node_env = [e for e in env if e['name'] == 'KURED_NODE_ID']
    require(len(node_env) == 1 and set(node_env[0]) == {'name', 'valueFrom'} and
            set(node_env[0]['valueFrom']) == {'fieldRef'} and
            node_env[0]['valueFrom']['fieldRef'].get('fieldPath') == 'spec.nodeName' and
            node_env[0]['valueFrom']['fieldRef'].get('apiVersion', 'v1') == 'v1' and
            set(node_env[0]['valueFrom']['fieldRef']) <= {'fieldPath', 'apiVersion'} and
            not container.get('envFrom') and
            not any(e['name'].startswith('KURED_') and e['name'] != 'KURED_NODE_ID' for e in env),
            'Kured environment requires only downward API KURED_NODE_ID')
    mounts = [m for m in container.get('volumeMounts', []) if m['mountPath'] == '/sentinel']
    volumes = {v['name']: v for v in spec.get('volumes', [])}
    require(len(mounts) == 1 and mounts[0].get('readOnly') is True and
            not mounts[0].get('subPath') and not mounts[0].get('subPathExpr') and
            volumes.get(mounts[0]['name'], {}).get('hostPath', {}).get('path') == '/var/lib/kured',
            'Kured sentinel hostPath mount missing or unsafe')
    status = ds['status']
    desired = status.get('desiredNumberScheduled', 0)
    require(desired > 0 and status.get('updatedNumberScheduled') == desired and
            status.get('numberAvailable') == desired and status.get('numberUnavailable', 0) == 0 and
            status.get('numberMisscheduled', 0) == 0, 'Kured rollout has not converged')
    targets = [p for p in pods if p.get('spec', {}).get('nodeName') == data['node'] and
               p['metadata'].get('namespace') == ds['metadata']['namespace'] and
               any(o.get('uid') == ds['metadata']['uid'] and o.get('controller') is True
                   for o in p['metadata'].get('ownerReferences', []))]
    require(len(targets) == 1, 'No unique healthy kured pod on target')
    running = targets[0]['spec']
    require(running.get('hostPID') == spec.get('hostPID') and
            not running.get('initContainers') and len(running.get('containers', [])) == 1,
            'Running kured pod differs from validated spec')
    actual = running['containers'][0]
    require(all(actual.get(key) == container.get(key) for key in
                ('name', 'image', 'command', 'args', 'env', 'envFrom', 'securityContext')) and
            all(m in actual.get('volumeMounts', []) for m in container.get('volumeMounts', [])) and
            [m for m in actual.get('volumeMounts', []) if m['mountPath'] == '/sentinel'] == mounts and
            all(v in running.get('volumes', []) for v in spec.get('volumes', [])),
            'Running kured pod differs from validated spec')
    lease = next(x for x in objects if x['kind'] == 'Lease' and
                 x['metadata']['namespace'] == 'kube-node-lease' and x['metadata']['name'] == data['node'])
    renewed = timestamp(lease['spec']['renewTime'])
    now = datetime.datetime.now(datetime.timezone.utc)
    require(datetime.timedelta(0) <= now - renewed < datetime.timedelta(seconds=60), 'Node lease is stale')
    boot = node['status']['nodeInfo']['bootID']
    if 'baseline' in data:
        require(version == data['baseline']['version'], 'Kubelet version changed during OS maintenance')
        require(renewed > timestamp(data['after']), 'No fresh post-maintenance lease')
        if data['reboot']:
            require(boot != data['baseline']['boot'], 'Waiting for actual kured boot-ID change')
    return {'version': version, 'boot': boot, 'lease': lease['spec']['renewTime']}


if __name__ == '__main__':
    try:
        print(json.dumps(check(json.load(sys.stdin))))
    except (ValueError, KeyError, StopIteration, TypeError) as error:
        sys.exit('Maintenance gate failed: ' + str(error))
