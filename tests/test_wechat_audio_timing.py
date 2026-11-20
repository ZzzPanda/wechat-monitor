"""Exercise the actual recorder's MKV-to-MP4 validation on external-disk fixtures."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'scripts'))
import capture_wechat_player as capture


def ffmpeg(*args, check=True):
    return subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', *args],
                          capture_output=True, text=True, check=check, timeout=15)


def packet_details(path, stream):
    result = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', stream, '-show_packets',
        '-show_data_hash', 'sha256', '-show_entries', 'packet=pts_time,data_hash', '-of', 'json', path,
    ], timeout=15)
    packets = json.loads(result)['packets']
    return [p['data_hash'] for p in packets], [float(p['pts_time']) for p in packets]


class RecordedAudioClock(unittest.TestCase):
    def test_bounded_pulse_burst_keeps_video_and_unaffected_pcm(self):
        with tempfile.TemporaryDirectory(dir=capture.BASE / 'tmp') as folder:
            folder = Path(folder)
            good, raw, ready = [folder / n for n in ('good.mkv', 'pulse.mkv', 'ready.mp4')]
            ffmpeg('-f', 'lavfi', '-i', 'color=size=64x64:rate=5:duration=50',
                   '-f', 'lavfi', '-i', 'sine=frequency=733:sample_rate=48000:duration=49.8',
                   '-c:v', 'libx264', '-bf', '0', '-af', 'asetpts=PTS+0.15/TB', '-c:a', 'aac', good)
            # Pulse compressed multiple consecutive starts, then continued from
            # a new clock offset about 100 ms earlier for the rest of the take.
            ffmpeg('-i', good, '-map', '0', '-c', 'copy', '-bsf:a',
                   "setts=ts='if(gte(N,700),max(PREV_OUTDTS,TS-104),TS)'", raw)
            packets = capture.read_aac_packets(raw, 5)
            with self.assertRaisesRegex(RuntimeError, 'overlap'):
                capture.audio_remux_options(packets)
            windows = capture.pulse_overlap_windows(packets)
            self.assertEqual(len(windows), 1)
            left, right, excess = windows[0]
            self.assertLessEqual(abs(excess / 48 - 104), 1)
            take = capture.LiveCapture(crop=(0, 0, 64, 64), duration=50, output=folder / 'output.mp4')
            take._raw = raw
            take._validate(ready)
            self.assertEqual(packet_details(raw, 'v:0'), packet_details(ready, 'v:0'))
            def pcm(path):
                return subprocess.check_output([
                    'ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', path, '-map', '0:a',
                    '-af', 'asetpts=N/SR/TB', '-f', 's24le', 'pipe:1'], timeout=10)
            x, y = pcm(raw), pcm(ready)
            priming = len(packets) - len(x) // 3 // 1024
            self.assertIn(priming, (0, 1))
            self.assertEqual(x[:(left-priming)*3072], y[:(left-priming)*3072])
            tail = x[(right-priming)*3072:]
            self.assertEqual(tail, y[-len(tail):])
            self.assertLessEqual(abs((len(x)-len(y))/3 - excess), 5)
            out = json.loads(subprocess.check_output([
                'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_streams', '-of', 'json', ready
            ]))['streams'][0]
            self.assertEqual(out['codec_name'], 'alac')
            self.assertAlmostEqual(float(out['start_time']), packets[priming] / 1000, places=4)
            self.assertGreater(float(out['start_time']), 0.1)
            # Forbid a real forward hole, real reversal, larger step, or near-edge change.
            for i, delta in ((900, 180), (900, -30), (700, -350)):
                bad = packets[:]
                for j in range(i, len(bad)):
                    bad[j] += delta
                self.assertEqual(capture.pulse_overlap_windows(bad), [])
            self.assertEqual(capture.pulse_overlap_windows(packets[670:]), [])

    def test_real_remux_negative_24_samples(self):
        with tempfile.TemporaryDirectory(dir=capture.BASE / 'tmp') as folder:
            folder = Path(folder)
            good, raw, ready = [folder / n for n in ('good.mkv', 'coalesced.mkv', 'ready.mp4')]
            ffmpeg('-f', 'lavfi', '-i', 'color=size=64x64:rate=24:duration=3',
                   '-f', 'lavfi', '-i', 'sine=sample_rate=48000:duration=2.8',
                   '-c:v', 'libx264', '-bf', '0', '-af', 'asetpts=PTS+0.15/TB', '-c:a', 'aac', good)
            self.assertEqual(capture.audio_remux_options(capture.read_aac_packets(good, 5)), [])
            ffmpeg('-i', good, '-map', '0', '-c', 'copy', '-bsf:a',
                   "setts=ts='if(eq(N,54),PREV_OUTDTS,TS)'", raw)
            failed = ffmpeg('-xerror', '-i', raw, '-map', '0', '-c', 'copy',
                            folder / 'original.mp4', check=False)
            self.assertNotEqual(failed.returncode, 0)
            match = re.search(r'Non-monotonic DTS; previous: (\d+), current: (\d+)', failed.stderr)
            self.assertIsNotNone(match, failed.stderr)
            self.assertEqual(int(match[1]) - int(match[2]), 24)
            take = capture.LiveCapture(crop=(0, 0, 64, 64), duration=3, output=folder / 'output.mp4')
            take._raw = raw
            take._validate(ready)
            ah, at = packet_details(raw, 'a:0')
            bh, bt = packet_details(ready, 'a:0')
            vh, vt = packet_details(raw, 'v:0')
            wh, wt = packet_details(ready, 'v:0')
            self.assertEqual(ah, bh)
            self.assertEqual(vh, wh)
            self.assertEqual(bt[:54] + bt[55:], at[:54] + at[55:])
            self.assertGreater(at[0] - vt[0], 0.1)
            self.assertAlmostEqual(bt[0] - wt[0], at[0] - vt[0], places=5)
            self.assertAlmostEqual(bt[54], at[55] - 1024 / 48000, places=5)
            for desc, index, stamp, delta, affected in [
                    ('same-8-samples', 53, 'PREV_OUTDTS', 8, 53),
                    ('adjacent-ticks', 54, 'PREV_OUTDTS+1', None, 54),
                    ('late-previous', 53, 'NEXT_PTS', None, 53)]:
                with self.subTest(desc=desc):
                    source, fixed = folder / (desc + '.mkv'), folder / (desc + '.mp4')
                    ffmpeg('-i', good, '-map', '0', '-c', 'copy', '-bsf:a',
                           f"setts=ts='if(eq(N,{index}),{stamp},TS)'", source)
                    if delta:
                        bad = ffmpeg('-xerror', '-i', source, '-c', 'copy',
                                     folder / (desc + '-original.mp4'), check=False)
                        m = re.search(r'Non-monotonic DTS; previous: (\d+), current: (\d+)', bad.stderr)
                        self.assertIsNotNone(m, bad.stderr)
                        self.assertEqual(int(m[1]) - int(m[2]), delta)
                    take._raw = source
                    take._validate(fixed)
                    ah, at = packet_details(source, 'a:0')
                    bh, bt = packet_details(fixed, 'a:0')
                    self.assertEqual(ah, bh)
                    self.assertEqual(bt[:affected] + bt[affected+1:], at[:affected] + at[affected+1:])
                    self.assertAlmostEqual(bt[affected] - bt[affected-1], 1024 / 48000, delta=.001)
            # A missing stretch must still fail before creating any MP4.
            gap = folder / 'gap.mkv'
            ffmpeg('-i', good, '-map', '0', '-c', 'copy', '-bsf:a',
                   "setts=ts='if(gte(N,54),TS+180,TS)'", gap)
            take._raw = gap
            with self.assertRaisesRegex(RuntimeError, 'discontinuity'):
                take._validate(folder / 'gap.mp4')
            self.assertFalse((folder / 'gap.mp4').exists())

    def test_reject_unbracketed_overlaps_reversals_and_missing_frames(self):
        for stamps in ([0, 21, 21], [0, 21, 21, 21, 85], [0, 21, 20, 64],
                       [0, 21, 17, 64], [0, 21, 64, 85], [0, 21, 21, 43]):
            with self.subTest(stamps=stamps), self.assertRaises(RuntimeError):
                capture.check_aac_clock(stamps)

    def test_long_timing_preserves_offsets_and_rejects_erratic_or_excessive_drift(self):
        count = 1520 * 48000 // 1024
        # A measured 25 ms clock difference over 25:20 still fits the 50 ms
        # limit and is preserved by the strict copy path.
        packets = [round(-21 + i * 1024 / 48 + 25 * i / count) for i in range(count)]
        self.assertFalse(capture.check_aac_clock(packets))
        packets[-10] = packets[-11]
        self.assertTrue(capture.check_aac_clock(packets))
        # A real 35-minute capture accumulated -72 ms despite every adjacent
        # pair being only 20–22 ms apart. Keep its original packet times.
        count = 99915
        smooth = [round(i * 1024 / 48 - 72 * i / count) for i in range(count)]
        self.assertEqual(capture.audio_remux_options(smooth), [])
        # A temporary loss of sync in the middle must not be explained by
        # the average clock rate, even with plausible individual intervals.
        middle = count // 2
        for i in range(middle - 100, middle + 100):
            smooth[i] += 100 - abs(i - middle)
        with self.assertRaisesRegex(RuntimeError, 'discontinuity'):
            capture.check_aac_clock(smooth)
        for i in range(len(packets)):
            packets[i] += round(200 * i / len(packets))
        with self.assertRaisesRegex(RuntimeError, 'discontinuity'):
            capture.check_aac_clock(packets)

    def test_long_stable_drift_can_shift_repair_window_without_loosening_sync_limit(self):
        burst = 23000
        packets = []
        for i in range(27000):
            loss = 0 if i < burst else max(103, 125 - max(0, i - burst - 6) * 2)
            ts = round(i * 1024 / 48 - 20 * min(1, i / burst) - loss)
            packets.append(max(ts, packets[-1]) if packets else ts)
        [(left, right, excess)] = capture.pulse_overlap_windows(packets)
        self.assertLess(left, burst - 144)
        for i, stamp in enumerate(packets):
            fraction = max(0, min(1, (i-left)/(right-left)))
            error = abs((stamp-packets[0])*48 - i*1024 + fraction*excess)
            self.assertLessEqual(error, 48*(75 if left <= i < right else 50))
        packets[burst+300] += 190
        self.assertFalse(capture.pulse_overlap_windows(packets))

    def test_long_bursts_can_absorb_verified_clock_skew_only(self):
        bursts = [8000, 23000, 32000, 39000, 44500, 46000, 52000]
        packets = []
        for i in range(57000):
            contraction = sum(max(104, 125-max(0, i-k-6)*2) for k in bursts if i >= k)
            stamp = round(i * 1024 / 48 * (1-.000034) - contraction)
            packets.append(max(packets[-1], stamp) if packets else stamp)
        windows = capture.pulse_overlap_windows(packets)
        self.assertEqual(len(windows), len(bursts))
        # The shift at the second window exceeds its intrinsic contraction
        # by the observed 10 ms skew across 320 seconds of normal frames.
        a, b, correction = windows[1]
        intrinsic = (b-a)*1024 - (packets[b]-packets[a])*48
        self.assertGreater(correction-intrinsic, 9*48)
        for a, b, correction in windows:
            self.assertLessEqual(correction, 125*48)
            self.assertLessEqual((b-a)*1024 / ((b-a)*1024-correction), 1.025)
        # A 25 ms non-linear interruption in that region has ordinary individual
        # packet spacings but cannot qualify as smooth oscillator drift.
        for i in range(10000, 12000):
            packets[i] -= min(25, (i-10000)*2, (12000-i)*2)
        self.assertFalse(capture.pulse_overlap_windows(packets))


if __name__ == '__main__':
    unittest.main()
