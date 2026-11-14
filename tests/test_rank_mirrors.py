from contextlib import redirect_stdout
import gc
import io
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'playbooks' / 'files'))
import rank_mirrors


class RankTests(unittest.TestCase):
    def test_fastest_measured_server_moves_first_and_the_rest_is_untouched(self):
        lines = ['# rated', 'Server = slow', 'Server = fast', '# note', 'Server = dead', 'Server = tail']
        self.assertEqual(rank_mirrors.rank(lines, [0.1, 20.0, 0.0]),
                         ['# rated', 'Server = fast', 'Server = slow', '# note', 'Server = dead', 'Server = tail'])

    def test_unreachable_server_measures_zero(self):
        self.assertEqual(rank_mirrors.measure('http://127.0.0.1:9/x.files', seconds=1), 0.0)

    def test_server_answering_garbage_measures_zero(self):
        server = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(server.close)

        def answer():
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(b'NOT HTTP\r\n\r\n')

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:%d/x.files' % server.getsockname()[1]
        self.assertEqual(rank_mirrors.measure(url, seconds=2), 0.0)
        thread.join(5)

    def test_invalid_server_url_measures_zero(self):
        self.assertEqual(rank_mirrors.measure('http://127.0.0.1:port/x.files', seconds=1), 0.0)


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=os.environ['TMPDIR'])
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.mirrorlist = self.directory / 'mirrorlist'
        self.mirrorlist.write_text('# rated\nServer = https://slow.example/$repo\nServer = https://fast.example/$repo\n')
        self.config = self.directory / 'pacman.conf'
        self.config.write_text('[options]\nArchitecture = auto\n[core]\nInclude = %s\n' % self.mirrorlist)
        servers = ['Server = https://slow.example/core', 'Server = https://fast.example/core']
        for name, value in (('pacman_conf', lambda *args: servers),
                            ('measure', lambda url: 10.0 if 'fast' in url else 1.0),
                            ('groups', lambda: {str(self.mirrorlist): 'core'})):
            patcher = patch.object(rank_mirrors, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self):
        output = io.StringIO()
        with redirect_stdout(output):
            rank_mirrors.main()
        return output.getvalue()

    def test_reorders_the_mirrorlist_in_place(self):
        self.assertTrue(self.run_main().endswith('REORDERED\n'))
        self.assertEqual(self.mirrorlist.read_text(),
                         '# rated\nServer = https://fast.example/$repo\nServer = https://slow.example/$repo\n')
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['mirrorlist', 'pacman.conf'])

    def test_failed_replace_keeps_the_original_and_leaves_no_temporary_file(self):
        original = self.mirrorlist.read_text()
        with patch.object(rank_mirrors.os, 'replace', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                self.run_main()
        self.assertEqual(self.mirrorlist.read_text(), original)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['mirrorlist', 'pacman.conf'])


class GroupsTests(unittest.TestCase):
    def test_maps_each_mirrorlist_to_its_first_repository_and_closes_the_file(self):
        with tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']) as directory:
            config = Path(directory) / 'pacman.conf'
            config.write_text('[options]\nInclude = /skip\n[core]\nInclude = /a\n[extra]\nInclude = /a\n'
                              '[cachyos]\nInclude = /b\n')
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                result = rank_mirrors.groups(str(config))
                gc.collect()
        self.assertEqual(result, {'/a': 'core', '/b': 'cachyos'})
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])


if __name__ == '__main__':
    unittest.main()
