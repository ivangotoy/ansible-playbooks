> [!NOTE]
> 🏡 **Home:** [git.digtvbg.com/ivangotoy/ansible-playbooks](https://git.digtvbg.com/ivangotoy/ansible-playbooks), where code, issues and releases live.
> 🪞 **Mirror:** [github.com/ivangotoy/ansible-playbooks](https://github.com/ivangotoy/ansible-playbooks), read-only, synced automatically.

# ansible-playbooks

Production-grade Ansible playbooks for Linux infrastructure management — Kubernetes nodes (CachyOS/Arch), mixed Debian/Arch environments, and homelab services.

## Structure

| Playbook | Target | Description |
|---|---|---|
| `cluster-perf-tuning.yml` | Kubernetes nodes | CPU governor, THP, network buffers, containerd tuning |
| `kubernetes-nodes-update-v2.yml` | Kubernetes nodes | Rolling OS and Kubernetes upgrade: drain, upgrade, reboot when needed |
| `refresh-mirrors.yml` | Kubernetes nodes | CachyOS mirror refresh via cachyos-rate-mirrors, then the first 5 servers of every mirrorlist ordered by measured download speed |
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
- `pipelining = true` — runs modules over the open SSH session without copying files first (connection reuse comes from `ControlPersist` in `ssh_args`)

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

**Phase 1 - Zero downtime (all nodes in parallel):**
- Sets `GOVERNOR='performance'` in `/etc/default/cpupower-service.conf` (the file the packaged `cpupower.service` reads), enables the service and restarts it when the file changed
- Disables Transparent Huge Pages (THP) via systemd tmpfiles and applies the file when it changed
- Applies network buffer sysctl tuning (`net.core.rmem_max`, `tcp_rmem`, `tcp_wmem`, etc.) in `/etc/sysctl.d/99-zz-k8s-perf.conf`, named to sort after other `99-*.conf` files so it wins at boot

**Phase 2 - Containerd tuning (serial restart):**
- Requires Python 3.11+ (`python3` with `tomllib`) and the native `containerd` executable on each node.
- Tunes explicit containerd config v2/v3 CRI tables, all `io.containerd.runc.v2` runtime option tables, and the v3 transfer-service download limit. Supports single/double quoted TOML table names.
- Supports verified empty optional imports using simple `*`/`?` filename globs in literal directories, resolved relative to the config directory. Rechecks imports after native validation. Rejects matching imports, literal imports (even missing files), other glob syntax, malformed imports, duplicate keys, malformed TOML, missing required tables and unsupported runtimes/layouts. Review these configurations manually.
- Parses both versions of the file and runs `containerd --config PATH config dump` on the original and candidate before replacement. Changed files receive a unique adjacent backup and atomic replacement; unchanged files are neither rewritten nor restarted.
- Restarts containerd only after a change, then checks CRI and kubelet health. Restart failure stops progression; backups permit manual recovery. This is not a guarantee of workload continuity, and changing a live cgroup driver needs an operator-reviewed maintenance plan. No reboot sentinel is created by this playbook.

### kubernetes-nodes-update-v2.yml

Rolling OS and Kubernetes upgrade of the `kubernetes` group:

```bash
ansible-playbook playbooks/refresh-mirrors.yml
ansible-playbook playbooks/kubernetes-nodes-update-v2.yml
```

1. **Download (all nodes in parallel):** syncs the pacman databases, downloads every pending repository package into the normal pacman cache and lists pending AUR upgrades. A node is also selected when a reboot is outstanding (`/run/reboot-required` exists or the image of the running kernel is gone) or when it still carries the `ansible-node-update/cordoned` annotation of an interrupted run; such a resumed node is drained, rebooted and uncordoned even with nothing pending. Other nodes are left alone; if no node is selected, nothing is drained and only the summary follows.
2. **Upgrade (one node at a time, control plane first, stops on the first failure):**
   - waits until the API answers `/readyz`, every node is Ready and every Deployment is Available
   - only when `kubeadm`/`kubelet` are pending: checks the version step (control plane at most one minor up, a worker never past the API server) before the node is touched
   - annotates the node with `ansible-node-update/cordoned` (unless it was cordoned before the run), then `kubectl drain --ignore-daemonsets --delete-emptydir-data` with a timeout; PodDisruptionBudgets are respected
   - only when `kubeadm`/`kubelet` are pending: installs `kubeadm` first, then runs `kubeadm upgrade apply` on the control plane (after an etcd snapshot and an `/etc/kubernetes` archive under `/var/lib/ansible-maintenance/control-plane-backup-*`) or `kubeadm upgrade node` on a worker
   - installs the downloaded upgrade with `pacman -Su`
   - reboots with `ansible.builtin.reboot` when the image of the running kernel is gone, `/run/reboot-required` exists or a boot-critical package was upgraded (`node_update_reboot_packages`) or the node is resumed from an interrupted run; otherwise restarts only containerd and the kubelet when their packages changed
   - waits until the node reports the new boot ID, the installed kubelet version and Ready, then uncordons it and removes the annotation. A node that was cordoned before the run (without the annotation) stays cordoned.
   - builds and installs pending AUR upgrades with `paru -Sua` after the node is back in service
3. **Summary:** one line per node: packages upgraded, rebooted yes/no, kubelet version, elapsed seconds.

Settings live in `group_vars/kubernetes.yml` (control plane host, node name mapping, timeouts incl. the request timeout of single kubectl calls, reboot package pattern). kubectl runs on the control plane host with `/etc/kubernetes/admin.conf`. The playbook does not use kured and never writes its sentinel.

After a failure no further node is touched: fix the cause and run the playbook again. A failure between the drain and the uncordon leaves the node cordoned with the annotation, and the next run resumes, reboots and uncordons it. A failure before the drain (waits, version step) leaves the node in service, and a failed AUR build happens after the uncordon; the next run picks such a node up again through its pending upgrades.

Offline checks (no managed host is contacted; the tests need `TMPDIR` set to an existing directory):
```bash
mkdir -p ~/.ansible/tmp
TMPDIR=~/.ansible/tmp python -m unittest discover -s tests
ansible-playbook --syntax-check playbooks/kubernetes-nodes-update-v2.yml
```

### update-upgrade.yml

OS-aware package upgrade for mixed environments:
- Auto-detects Debian vs Arch via `ansible_facts['os_family']`
- Groups hosts dynamically at runtime
- Debian cache refresh uses blocking async polling; only successful completion permits the dist-upgrade launch. Failures and timeouts stop that host.
- Every package job runs async with polling so slow upgrades survive SSH timeouts; the upgrade jobs get 1 hour, because an expired async job kills the package manager mid-transaction

### update-jenkins-plugins.yml

Automated Jenkins plugin updates via REST API:
- Fetches plugin list and filters only plugins with available updates
- Updates outdated plugins via `community.general.jenkins_plugin`, then restarts Jenkins once so they load
- Runs on the Jenkins host and talks to it on `127.0.0.1:8090`
- Requires `vault_jenkins_api_token` via Ansible Vault - never hardcode credentials
```yaml
# vars/vault.yml (ansible-vault encrypted)
vault_jenkins_api_token: "your-token-here"
```

## Secrets Management

Sensitive values (API tokens, passwords) are never stored in plaintext. Use Ansible Vault:
```bash
ansible-vault create vars/vault.yml
ansible-playbook playbooks/update-jenkins-plugins.yml -e @vars/vault.yml --ask-vault-pass
```

## Linting
```bash
ansible-lint playbooks/
```

## Environment

- **Kubernetes nodes:** CachyOS (Arch-based), managed with `paru`
- **Web servers:** Mixed Debian/Arch
- **Privilege escalation:** `doas`
- **SSH:** Ed25519 keys, ControlMaster persistent connections
