"""Offline tests of the Debian polling in update-upgrade.yml."""
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class DebianPollingTests(unittest.TestCase):
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
