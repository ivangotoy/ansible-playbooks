Ansible for home use/learning purposed + initial setup:

1. python venv: cd ; python -m venv ansible-learning

2. shell alias in ~/.bashrc or ~/.zshrc to enter the Venv: alias alearn=". ${HOME}/ansible-learning/bin/activate"

3. reminder for the alias in .zshrc/.bashrc printf "type alearn to enter ansible venv\n"

3. activate the venv: type alearn

4. install useful packages in the venv: pip install --no-cache-dir -U ansible ansible-lint mitogen pip

5. check ansible version: ansible --version

6. create ansible.cfg , inventory playbooks directory , playbook files etc.

7. check playbooks with ansible-lint e.g. ansible-lint my-super-cool-playbook.yml and die from cringe

8. setup redis: apt install valkey && systemctl enable --now valkey

9. create ansible user on all nodes with proper permissions

10. generate ansible ssh ed25519 key pair , put the public part of the key on all nodes that need to be contorlled with ansible (in ansible user ~/.ssh/authorized_keys)
