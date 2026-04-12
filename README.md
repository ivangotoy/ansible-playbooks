> ⚠️ **Mirror.** Primary repository: [git.digtvbg.com](https://git.digtvbg.com/ivangotoy/ansible-playbooks)
> Development, issues, and PRs happen there. The GitHub repo is read-only.

# ansible-playbooks

Production-grade Ansible playbooks for Linux infrastructure management — Kubernetes nodes (CachyOS/Arch), mixed Debian/Arch environments, and homelab services.

## Structure

| Playbook | Target | Description |
|---|---|---|
| `cluster-perf-tuning.yml` | Kubernetes nodes | CPU governor, THP, network buffers, containerd tuning |
| `kubernetes-nodes-update-v2.yml` | Kubernetes nodes | Rolling package upgrades via paru (serial, safe) |
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

**Phase 2 — Containerd tuning (serial, shim-safe):**
- Patches `config.toml` for `SystemdCgroup = true` and `max_concurrent_downloads = 10`
- Restarts containerd without affecting running pods (shim-safe)
- Validates kubelet health after each node

Reboots are handled externally by [kured](https://github.com/kubereboot/kured) via sentinel file — this playbook never reboots nodes directly.

### kubernetes-nodes-update-v2.yml

Safe rolling upgrade for CachyOS/Arch Kubernetes nodes:
- `serial: 1` — one node at a time
- Updates `archlinux-keyring` first to avoid GPG failures
- Checks for available upgrades before running — skips if nothing to do
- Kured handles reboots if kernel was updated

### update-upgrade.yml

OS-aware package upgrade for mixed environments:
- Auto-detects Debian vs Arch via `ansible_facts['os_family']`
- Groups hosts dynamically at runtime
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
