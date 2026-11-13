import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playbooks' / 'files'))
import rank_mirrors


class RankTests(unittest.TestCase):
    def test_fastest_measured_server_moves_first_and_the_rest_is_untouched(self):
        lines = ['# rated', 'Server = slow', 'Server = fast', '# note', 'Server = dead', 'Server = tail']
        self.assertEqual(rank_mirrors.rank(lines, [0.1, 20.0, 0.0]),
                         ['# rated', 'Server = fast', 'Server = slow', '# note', 'Server = dead', 'Server = tail'])

    def test_unreachable_server_measures_zero(self):
        self.assertEqual(rank_mirrors.measure('http://127.0.0.1:9/x.files', seconds=1), 0.0)


if __name__ == '__main__':
    unittest.main()
