"""Deterministic Kubernetes transition validation; no cluster mutations."""
import json
import re
import sys

PACKAGES = ('kubeadm', 'kubelet', 'kubectl')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    require(match is not None, 'Unsupported Kubernetes version: ' + value)
    return tuple(map(int, match.groups()))


def package(value):
    # Restrict to the numeric upstream versions/releases used by these packages.
    # Epochs and vendor builds need explicit compatibility review, not guessing.
    match = re.fullmatch(r'(\d+\.\d+\.\d+)-(\d+(?:\.\d+)*)', value)
    require(match is not None, 'Unsupported Kubernetes package version: ' + value)
    return version(match[1]), tuple(map(int, match[2].split('.')))


def path(current, target, label):
    require(current <= target, label + ': downgrade rejected')
    require(current[0] == target[0], label + ': major transition rejected')
    require(target[1] - current[1] <= 1, label + ': skipped minor rejected')


def plan(data):
    nodes = data['nodes']
    require(bool(nodes), 'No managed nodes')
    hosts = [n['host'] for n in nodes]
    names = [n['node'] for n in nodes]
    uids = [n['uid'] for n in nodes]
    require(len(set(hosts)) == len(hosts) and set(hosts) == set(data['inventory']),
            'Incomplete or duplicate inventory mapping')
    require(len(set(names)) == len(names) and set(names) == set(data['cluster_nodes']),
            'Incomplete or duplicate Kubernetes node mapping')
    require(all(uids) and len(set(uids)) == len(uids), 'Missing or duplicate node UID')
    require(sum(n['control_plane'] is True for n in nodes) == 1,
            'Exactly one control plane required')
    require(all(n['schedulable'] is True for n in nodes), 'Pre-cordoned node rejected')
    candidates = nodes[0]['candidates']
    require(set(candidates) == set(PACKAGES), 'Missing Kubernetes candidates')
    targets = {package(candidates[p])[0] for p in PACKAGES}
    require(len(targets) == 1, 'Kubernetes candidates have different target versions')
    target = targets.pop()
    target_text = 'v' + '.'.join(map(str, target))
    api = version(data['api'])
    path(api, target, 'API')
    result = []
    for node in sorted(nodes, key=lambda n: not n['control_plane']):
        require(node['candidates'] == candidates, 'Repository candidate mismatch: ' + node['host'])
        require(set(node['installed']) == set(PACKAGES), 'Missing installed Kubernetes package')
        installed = {p: package(node['installed'][p]) for p in PACKAGES}
        for name, (current, release) in installed.items():
            path(current, target, node['host'] + '/' + name)
            require((current, release) <= package(candidates[name]),
                    node['host'] + '/' + name + ': package downgrade rejected')
        reported = version(node['reported'])
        path(reported, target, node['host'] + '/reported kubelet')
        require(reported <= api, 'Reported kubelet newer than API')
        require(reported <= installed['kubelet'][0], 'Reported kubelet newer than installed package')
        pending = node.get('pending')
        if pending is not None:
            require(isinstance(pending, dict) and
                    all(pending.get(k) == v for k, v in dict(
                        target=target_text, candidates=candidates, node=node['node'], uid=node['uid']).items()) and
                    type(pending.get('apply_complete')) is bool,
                    'Pending maintenance target/identity mismatch: ' + node['host'])
        reconcile = reported != target or any(v[0] != target for v in installed.values())
        apply = False
        if node['control_plane']:
            config = version(node['cluster_config'])
            path(config, target, 'kubeadm ClusterConfiguration')
            apply = (api != target or config != target or
                     (not pending['apply_complete'] if pending is not None else reconcile))
        result.append(dict(host=node['host'], node=node['node'], uid=node['uid'],
                           control_plane=node['control_plane'],
                           apply=apply,
                           packages=[p for p in PACKAGES if node['installed'][p] != candidates[p]],
                           reconcile=reconcile or pending is not None or apply))
    return dict(target='v' + '.'.join(map(str, target)), candidates=dict(candidates),
                upgrade_api=api != target, nodes=result)


if __name__ == '__main__':
    try:
        print(json.dumps(plan(json.load(sys.stdin))))
    except (ValueError, KeyError, TypeError) as error:
        sys.exit('Transition rejected: ' + str(error))
