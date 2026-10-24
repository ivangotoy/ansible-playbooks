> ⚠️ **Mirror.** Primary repository: [git.digtvbg.com](https://git.digtvbg.com/ivangotoy/ansible-playbooks)
> Development, issues, and PRs happen there. The GitHub repo is read-only.

# ansible-playbooks

Production-grade Ansible playbooks for Linux infrastructure management — Kubernetes nodes (CachyOS/Arch), mixed Debian/Arch environments, and homelab services.

## Structure

| Playbook | Target | Description |
|---|---|---|
| `cluster-perf-tuning.yml` | Kubernetes nodes | CPU governor, THP, network buffers, containerd tuning |
| `kubernetes-nodes-update-v2.yml` | Kubernetes nodes | Guarded OS maintenance via paru and kured |
| `kubernetes-nodes-update.yml` | Kubernetes nodes | Parallel package upgrades via paru |
| `refresh-mirrors.yml` | Kubernetes nodes | CachyOS mirror refresh via cachyos-rate-mirrors |
| `update-upgrade.yml` | Mixed (Debian/Arch) | OS-aware package upgrades with async execution |
| `update-jenkins-plugins.yml` | Jenkins host | Automated Jenkins plugin updates via API |

## Requirements
```bash
pip install ansible ansible-lint
ansible-galaxy collection install -r requirements.yml
```

## Configuration

Copy and adapt `ansible.cfg` to your environment. Key settings:

- `become_method = doas` — uses doas instead of sudo
- `forks = 50` — parallel execution
- `fact_caching = jsonfile` — caches facts for 1 hour
- `pipelining = true` — SSH connection reuse for performance

Create a dedicated `ansible` user on all managed nodes with your SSH public key in `~/.ssh/authorized_keys`.

## Inventory
```ini
[webservers]
host1 ansible_host=host1
host2 ansible_host=host2

[kubernetes]
node-01 ansible_host=node-01
node-02 ansible_host=node-02
node-03 ansible_host=node-03
kube    ansible_host=kube
```

## Playbook Details

### cluster-perf-tuning.yml

Two-phase playbook targeting Kubernetes node performance:

**Phase 1 — Zero downtime (all nodes in parallel):**
- Sets CPU governor to `performance` via cpupower, persisted across reboots
- Disables Transparent Huge Pages (THP) at runtime and via systemd tmpfiles
- Applies network buffer sysctl tuning (`net.core.rmem_max`, `tcp_rmem`, `tcp_wmem`, etc.)

**Phase 2 — Containerd tuning (serial restart):**
- Requires Python 3.11+ (`python3` with `tomllib`) and the native `containerd` executable on each node.
- Tunes explicit containerd config v2/v3 CRI tables, all `io.containerd.runc.v2` runtime option tables, and the v3 transfer-service download limit. Supports single/double quoted TOML table names.
- Supports verified empty optional imports using simple `*`/`?` filename globs in literal directories, resolved relative to the config directory. Rechecks imports after native validation. Rejects matching imports, literal imports (even missing files), other glob syntax, malformed imports, duplicate keys, malformed TOML, missing required tables and unsupported runtimes/layouts. Review these configurations manually.
- Parses both versions of the file and runs `containerd --config PATH config dump` on the original and candidate before replacement. Changed files receive a unique adjacent backup and atomic replacement; unchanged files are neither rewritten nor restarted.
- Restarts containerd only after a change, then checks CRI and kubelet health. Restart failure stops progression; backups permit manual recovery. This is not a guarantee of workload continuity, and changing a live cgroup driver needs an operator-reviewed maintenance plan. No reboot sentinel is created by this playbook.

### kubernetes-nodes-update-v2.yml

This is full CachyOS/Arch **OS maintenance**, not a Kubernetes version upgrade. It never runs `kubeadm upgrade`. It excludes protected Kubernetes packages from `paru -Syu`, requires installed `kubeadm`, `kubelet` and `kubectl` binaries to match the node version, and checks package versions before/after maintenance and after reboot. The API server and kubelet must share a minor version; more permissive skew policies are deliberately unsupported.

Prerequisites (provision separately; this playbook does not configure kured):
- Observations run on controller localhost with `become: false` and the existing Ansible venv Python. Aimax needs no local kubectl or kubeconfig. The default `maintenance_ssh_argv` uses bounded batch SSH with `/home/ivan/.ssh/ansible` to `ansible@kube`; `maintenance_kubectl_argv` runs `doas kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s`. Both argv lists are configurable; remote arguments are individually shell-quoted. Reads/uncordon have a 30-second outer timeout, drain 330 seconds. The route needs resource-read, cordon/uncordon and eviction privileges and synchronized clocks. Inventory `kube` maps to `kube-controller`; `node-01/02/03` keep their names. Override `maintenance_node` if needed; host and Kubernetes boot IDs must match. Verified topology uses Kubernetes binaries/packages v1.37.1.
- Provision disk-backed `/var/lib/ansible-maintenance/tmp` writable by the Ansible connection user on targets. Controller staging uses the supplied disk-backed `TMPDIR`; set `ANSIBLE_LOCAL_TEMP` beneath it.
- Nodes must have `paru`, the existing `aur_user` privileges, a working pacman trust/keyring state, and the exact protected package names above. Package/lock failures stop maintenance; no preliminary keyring transaction bypasses the drain.
- All nodes and non-completed pods must be healthy; Deployment, StatefulSet and DaemonSet replicas must be ready. Pre-cordoned nodes are rejected without changing their cordon. Resolve ownership separately before retrying.
- Supported policy is Helm chart 6.1.0, image 1.23.0, namespace/DaemonSet `kured/kured`, provisioned separately by the parent. Set `forceReboot: false` and host `rebootSentinelFile: /var/lib/kured/reboot-required`. The effective container sentinel flag is `--reboot-sentinel=/sentinel/reboot-required`. Required flags also include `--period=1m`, `--ds-name=kured`, `--ds-namespace=kured`. Accepted existing flags are `--metrics-port=8080`, `--drain-timeout=10m`, `--reboot-command=/bin/systemctl reboot`, `--time-zone=Europe/Sofia`, `--annotate-nodes=true`, `--prefer-no-schedule-taint=weave.works/kured-node-reboot`, `--log-format=text`, `--concurrency=1`, and optional `--force-reboot=false`. Other overrides are rejected. `KURED_NODE_ID` must come exclusively from downward API `spec.nodeName`; other `KURED_*` variables and envFrom are rejected.
- Kured must have `hostPID: true`, one privileged container with `readOnlyRootFilesystem: true`, no sidecars/init containers, and explicit command `/usr/bin/kured` or `/kured`. Mount host `/var/lib/kured` at `/sentinel` with `readOnly: true` and no subpath. Kured reads the sentinel; the host creates it. Reserve the sentinel for this workflow, with no package hooks or other writers. An existing request blocks preflight. Do not run overlapping maintenance controllers. The guard requires a converged rollout and the actual target pod to match the validated image, command, arguments, environment, security context and mounts.
- Provision `/etc/tmpfiles.d/kured-maintenance.conf` containing exactly `r! /var/lib/kured/reboot-required - - - -`, and have `systemd-tmpfiles-setup.service` active after boot. This boot-only cleanup is essential because the disk sentinel survives a reboot. The play checks both prerequisites and verifies sentinel removal on return.
- Native kured draining can discard emptyDir data. The play therefore requires explicit `maintenance_delete_emptydir_data=true` consent before any mutation; its default is false and fails closed. This also enables `--delete-emptydir-data` on the initial drain. No `--force` drain or forced reboot is requested. Unmanaged pods and PDB restrictions may prevent completion.

The play uses `serial: 1` and `any_errors_fatal: true`. Initial drain uses eviction with a five-minute deadline. After the package transaction it restores schedulability so standard kured can take ownership of its own drain/uncordon cycle. Workloads may schedule during that handoff. Any package delta (conservatively, not just a kernel update) or the independent OS pending-reboot marker requests a kured reboot. Kured retains reboot ownership; the play does not invoke a reboot command.

Before proceeding it requires an actual boot-ID change when requested, Ready state, a fresh post-maintenance node Lease, the unchanged kubelet version and cluster workload health. There are at most 60 health observations with 15-second intervals, bounded API requests, then a bounded connection check. Initial API/SSH errors fail preflight; during return observation, unavailable control-plane reads are retried within the same bounded loop while `kube` reboots. A persistent success boolean stops further observations after the first healthy return. PDBs, kured locks, access failures or unhealthy workloads can stop completion. Failure leaves recovery to the operator: inspect the node, cordon, packages, kured lock/logs and sentinel, reconcile any outstanding reboot, and restore health before rerunning. This workflow requires a maintenance window and does not guarantee completion or zero downtime.

Offline verification (existing environment; no managed-host execution):
```bash
ANSIBLE_LOCAL_TEMP="$TMPDIR/ansible-local" /home/ivan/ansible-learning-new/bin/python -B -m unittest discover -s tests -v
ANSIBLE_LOCAL_TEMP="$TMPDIR/ansible-local" /home/ivan/ansible-learning-new/bin/python -B -m ansible.cli.playbook --syntax-check -i 'localhost,' playbooks/cluster-perf-tuning.yml playbooks/update-upgrade.yml playbooks/kubernetes-nodes-update-v2.yml
```
Use a supplied disk-backed `TMPDIR` and `PYTHONPYCACHEPREFIX`. The synthetic fixtures cover config editing and health gates; real local Ansible include execution tests control-plane read failure/recovery, safe argv quoting, node mapping and skipped includes after success. The SSH endpoint is mocked: tests use no network or managed nodes. Debian polling is tested with mocked actions. These tests and syntax checks are **not real maintenance proof**. Production kured configuration, image behavior, RBAC, package protection and recovery still need validation in an authorized maintenance environment.

### update-upgrade.yml

OS-aware package upgrade for mixed environments:
- Auto-detects Debian vs Arch via `ansible_facts['os_family']`
- Groups hosts dynamically at runtime
- Debian cache refresh uses blocking async polling; only successful completion permits the dist-upgrade launch. Failures and timeouts stop that host.
- Uses async execution with polling to avoid SSH timeouts on slow upgrades

### update-jenkins-plugins.yml

Automated Jenkins plugin updates via REST API:
- Fetches plugin list and filters only plugins with available updates
- Updates outdated plugins in parallel via `community.general.jenkins_plugin`
- Requires `vault_jenkins_api_token` via Ansible Vault — never hardcode credentials
```yaml
# vars/vault.yml (ansible-vault encrypted)
vault_jenkins_api_token: "your-token-here"
```

## Secrets Management

Sensitive values (API tokens, passwords) are never stored in plaintext. Use Ansible Vault:
```bash
ansible-vault create vars/vault.yml
ansible-playbook update-jenkins-plugins.yml --ask-vault-pass
```

## Linting
```bash
ansible-lint *.yml
```

## Environment

- **Kubernetes nodes:** CachyOS (Arch-based), managed with `paru`
- **Web servers:** Mixed Debian/Arch
- **Privilege escalation:** `doas`
- **SSH:** Ed25519 keys, ControlMaster persistent connections Sonnet 4.6

The health guard also supports read-only standalone preflight, without emptyDir consent or maintenance execution. Pipe one JSON object with `resources` (the resource List from the read command), `version` (kubectl version JSON), and `node` (`kube-controller` or a worker name) into `/home/ivan/ansible-learning-new/bin/python -B playbooks/files/maintenance_health.py`. Do not include `baseline`, `after` or `reboot` for initial preflight. It emits the original boot/version/lease on success and exits nonzero on unsafe configuration or unhealthy state. Parent live preflight remains separate from these offline checks.
