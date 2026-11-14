"""Offline tests of the async limits in update-upgrade.yml."""
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYS = yaml.safe_load((ROOT / 'playbooks/update-upgrade.yml').read_text())


class AsyncTests(unittest.TestCase):
    def test_debian_refreshes_the_cache_before_the_upgrade(self):
        tasks = PLAYS[1]['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertTrue(tasks[0]['ansible.builtin.apt']['update_cache'])
        self.assertEqual(tasks[1]['ansible.builtin.apt']['upgrade'], 'dist')

    def test_upgrades_block_on_the_job_and_get_an_hour(self):
        for play in PLAYS[1:]:
            for task in play['tasks']:
                with self.subTest(task=task['name']):
                    self.assertGreater(task['poll'], 0)
                    self.assertNotIn('ignore_errors', task)
                    self.assertNotIn('failed_when', task)
        for task in (PLAYS[1]['tasks'][1], PLAYS[2]['tasks'][0]):
            self.assertGreaterEqual(task['async'], 3600)


if __name__ == '__main__':
    unittest.main()
