"""Tests for tools/pull.py. Run from the repository root: python3 -m unittest discover tests"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PULL = os.path.join(ROOT, 'tools', 'pull.py')
spec = importlib.util.spec_from_file_location('pull', PULL)
pull = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pull)

EVENTS = ('time,target,event,duration_s,detail\n'
          '2026-10-01T16:00:00.000+02:00,monitor,start,,iface=eth0\n'
          '2026-10-01T17:00:00.000+02:00,1.1.1.1,down,,\n'
          '2026-10-01T17:00:30.000+02:00,1.1.1.1,up,30,\n')
MORE = ('2026-10-02T17:00:00.000+02:00,1.1.1.1,down,,\n'
        '2026-10-02T17:00:30.000+02:00,1.1.1.1,up,30,\n')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def read(path):
    with open(path) as f:
        return f.read()


class Source(unittest.TestCase):
    def test_split_source(self):
        self.assertEqual(pull.split_source('pi@linemon.local:/var/lib/linemon'), ('pi@linemon.local', '/var/lib/linemon'))
        self.assertEqual(pull.split_source('linemon:/var/lib/linemon'), ('linemon', '/var/lib/linemon'))
        self.assertEqual(pull.split_source('/mnt/pi/linemon'), (None, '/mnt/pi/linemon'))
        self.assertEqual(pull.split_source('./data:old'), (None, './data:old'))

    def test_sizes_are_listed_by_the_code_sent_to_the_pi(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        write(os.path.join(tmp, 'events.csv'), EVENTS)
        write(os.path.join(tmp, 'captures', '20261001T170604-outage-start', 'status.xml'), '<x/>')
        self.assertEqual(pull.source_sizes(None, tmp), {
            'events.csv': len(EVENTS), os.path.join('captures', '20261001T170604-outage-start', 'status.xml'): 4})


@unittest.skipUnless(shutil.which('rsync'), 'needs rsync')
class Pull(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, 'pi')
        self.dest = os.path.join(self.tmp, 'backup')
        write(os.path.join(self.src, 'events.csv'), EVENTS)
        write(os.path.join(self.src, 'minute.csv'), 'minute,target,sent,lost,rtt_avg_ms,rtt_max_ms\n')
        write(os.path.join(self.src, 'health.json'), '{"state": "healthy"}\n')
        write(os.path.join(self.src, 'captures', '20261001T170604-outage-start', 'status.xml'), '<x/>')

    def run_pull(self, stamp):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            kept = pull.pull(self.src, self.dest, stamp)
        return kept, out.getvalue()

    def test_copies_then_appends(self):
        self.assertEqual(self.run_pull('1')[0], None)
        self.assertEqual(read(os.path.join(self.dest, 'events.csv')), EVENTS)
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'captures', '20261001T170604-outage-start', 'status.xml')))
        with open(os.path.join(self.src, 'events.csv'), 'a') as f:
            f.write(MORE)
        health = os.path.join(self.src, 'health.json')
        write(health, '{"state": "unknown"}\n')  # rewritten at the same size, as every 15 s on the Pi
        os.utime(health, (os.path.getmtime(health) + 15,) * 2)
        self.assertEqual(self.run_pull('2')[0], None)
        self.assertEqual(read(os.path.join(self.dest, 'events.csv')), EVENTS + MORE)
        self.assertEqual(read(os.path.join(self.dest, 'health.json')), '{"state": "unknown"}\n')

    def test_keeps_a_file_that_got_smaller(self):
        """trim.py shrinks files; the backup must not lose what it deleted."""
        with open(os.path.join(self.src, 'events.csv'), 'a') as f:
            f.write(MORE)
        self.run_pull('1')
        write(os.path.join(self.src, 'events.csv'), 'time,target,event,duration_s,detail\n' + MORE)  # trimmed
        kept, out = self.run_pull('20261005T090000')
        self.assertEqual(kept, self.dest + '-kept-20261005T090000')
        self.assertEqual(read(os.path.join(kept, 'events.csv')), EVENTS + MORE)  # the old copy, untouched
        self.assertEqual(read(os.path.join(self.dest, 'events.csv')), read(os.path.join(self.src, 'events.csv')))
        self.assertIn('events.csv got smaller', out)
        self.assertEqual(os.listdir(kept), ['events.csv'])  # only what shrank

    def test_never_deletes_from_the_backup(self):
        self.run_pull('1')
        shutil.rmtree(os.path.join(self.src, 'captures'))  # e.g. trim.py removed old capture folders
        self.run_pull('2')
        self.assertTrue(os.path.exists(os.path.join(self.dest, 'captures', '20261001T170604-outage-start', 'status.xml')))

    def test_skips_half_written_files(self):
        write(os.path.join(self.src, 'health.json.tmp'), '{"sta')
        self.run_pull('1')
        self.assertFalse(os.path.exists(os.path.join(self.dest, 'health.json.tmp')))

    def test_command_line_checks_the_copy(self):
        r = subprocess.run([sys.executable, PULL, self.src, self.dest], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Checked: analyze.py reads the copy.', r.stdout)


class WithoutRsync(unittest.TestCase):
    def test_says_rsync_is_missing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        r = subprocess.run([sys.executable, PULL, tmp, os.path.join(tmp, 'backup')], capture_output=True, text=True,
                           env=dict(os.environ, PATH=''))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('rsync is not installed', r.stderr)
        self.assertFalse(os.path.exists(os.path.join(tmp, 'backup')))


if __name__ == '__main__':
    unittest.main()
