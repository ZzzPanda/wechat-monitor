from pathlib import Path
import runpy
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'scripts'))
from wechat_frames import FrameTime
entry = runpy.run_path(str(ROOT / 'scripts' / 'wechat-save'))


class FrameClock(unittest.TestCase):
    def test_guest_drift_and_work_in_the_delivery_pipe(self):
        # Over 25 minutes a guest running 500 ppm fast gains 0.76s. A
        # one-time offset produces a physically impossible future acquisition.
        origin = 200.0
        for elapsed in (1., 14., 1520.):
            host = origin + elapsed
            guest = 10000. + elapsed*1.0005
            stamp = FrameTime(host, host+.050, guest+.002, guest+.009,
                              guest+.012, 1800000000.+elapsed, guest+.047)
            self.assertAlmostEqual(stamp, host+.010, delta=.003)
            self.assertLess(stamp, host+.050)
            self.assertEqual(stamp.guest_wall, 1800000000.+elapsed)
        with self.assertRaises(TimeoutError):
            FrameTime(host, host+.500, guest, guest+.004, guest+.005, 1., guest+.010)
        with self.assertRaises(RuntimeError):
            FrameTime(host, host+.050, guest, guest+.040, guest+.030, 1., guest+.045)

    def test_cut_uses_media_clock_and_requires_observed_transition(self):
        wall = 1800000000.0
        evidence = dict(duration=1520, restart=100.15, origin=100.0, video_wall_origin=wall,
                        tail=dict(end_monotonic=1620.25, first_change_monotonic=1620.65,
                                  start_guest_wall=wall+.16, end_guest_wall=wall+1519.65,
                                  first_change_guest_wall=wall+1520.04))
        start, end = entry['evidence_bounds'](evidence)
        self.assertAlmostEqual(start, .16, places=5)
        self.assertAlmostEqual(end, 1519.65, places=5)
        for missing in ('video_wall_origin', 'first_change_guest_wall'):
            bad = dict(evidence, tail=dict(evidence['tail']))
            (bad if missing == 'video_wall_origin' else bad['tail']).pop(missing)
            with self.assertRaises(entry['SaveError']):
                entry['evidence_bounds'](bad)
        evidence['tail']['first_change_guest_wall'] += 1.
        with self.assertRaises(entry['SaveError']):
            entry['evidence_bounds'](evidence)


if __name__ == '__main__':
    unittest.main()
