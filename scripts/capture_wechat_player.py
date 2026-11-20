#!/usr/bin/env python3
"""Capture one selected WeChat player region and the container's playback monitor."""

import argparse
from array import array
from collections import deque
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from wechat_paths import DATA_ROOT as BASE, DISK_UUID, DOCKER_HOST, check_drive

DOCKER = ['docker', '--host', DOCKER_HOST]
CONTAINER = 'wx-playback'

# stdin is a dedicated host control pipe. stdout contains only Matroska bytes.
GUEST = r"""
import ctypes, json, os, re, select, signal, subprocess as sp, sys, time
window, x, y, width, height, seconds, fps, mode = sys.argv[1:]
x, y, width, height = map(int, (x, y, width, height))
os.environ['LC_ALL'] = 'C'

def read(*command):
    return sp.check_output(command, text=True, stderr=sp.DEVNULL, timeout=3)

def active():
    return int(read('xprop', '-root', '_NET_ACTIVE_WINDOW').split()[-1], 16)

def identity():
    return read('xprop', '-id', hex(win), 'WM_CLASS', '_NET_WM_PID', '_NET_WM_NAME', 'WM_NAME')

def geometry():
    data = read('xwininfo', '-id', hex(win))
    return (int(re.search(r'Width:\s*(\d+)', data)[1]),
            int(re.search(r'Height:\s*(\d+)', data)[1]), 'IsViewable' in data)

def audio_guard():
    streams = json.loads(read('pactl', '-f', 'json', 'list', 'sink-inputs'))
    for stream in streams:
        if str(stream['sink']) != output_index or stream.get('corked') or stream.get('mute'):
            continue
        props = stream.get('properties', {})
        apps = [str(props.get(key, '')).lower() for key in ('application.process.binary', 'application.name')]
        if not any(app in ('wechatappex', 'wechat', 'weixin') for app in apps):
            sys.exit('Another application is playing on the capture sink.')

win = active() if window == 'auto' else int(window, 0)
ident = identity()
size = geometry()
gw, gh, visible = size
if not visible or active() != win or 'WeChatAppEx' not in ident:
    sys.exit('The foreground window must be the visible WeChatAppEx player.')
if min(x,y)<0 or width<16 or height<16 or width%2 or height%2 or x+width>gw or y+height>gh:
    sys.exit('The requested picture rectangle is outside the player.')
if (width,height) == (gw,gh):
    sys.exit('Select the programme picture inside the window.')
sources = [s.split('\t')[1] for s in read('pactl','list','short','sources').splitlines()]
sinks = [s.split('\t') for s in read('pactl','list','short','sinks').splitlines()]
if 'output.monitor' not in sources or not any(s[1] == 'output' for s in sinks):
    sys.exit('The container playback output.monitor is unavailable.')
output_index = next(s[0] for s in sinks if s[1] == 'output')
audio_guard()
print(f'WX_WINDOW={win}', file=sys.stderr, flush=True)
if mode == 'check':
    sys.exit(0)

def interrupted(signum, frame):
    raise SystemExit('Capture interrupted.')

for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, interrupted)
parent = os.getpid()
libc = ctypes.CDLL(None)

def child_lifecycle():
    # Stop this ffmpeg even when the recording helper itself is killed abruptly.
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(125)

command = ['/usr/bin/ffmpeg', '-nostdin','-hide_banner','-loglevel','info',
           '-nostats','-stats_period','0.25','-progress','pipe:2',
           '-thread_queue_size','128','-f','x11grab','-framerate',fps,'-draw_mouse','0',
           '-window_id',str(win),'-video_size',f'{width}x{height}','-grab_x',str(x),'-grab_y',str(y),
           '-i',os.environ.get('DISPLAY',':1'),
           '-thread_queue_size','256','-f','pulse','-sample_rate','48000','-channels','2','-i','output.monitor',
           '-map','0:v:0','-map','1:a:0','-t',seconds,'-c:v','libx264','-threads','2',
           '-preset','ultrafast','-crf','18','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k',
           '-f','matroska','pipe:1']
print(f'WX_GUEST_WALL={time.time():.9f}', file=sys.stderr, flush=True)
proc = sp.Popen(command, stdin=sp.DEVNULL, preexec_fn=child_lifecycle)
try:
    started = last_heartbeat = time.monotonic()
    next_guard = next_audio = started
    pending = b''
    stopped = False
    while proc.poll() is None:
        now = time.monotonic()
        if now-started > float(seconds)+25 or now-last_heartbeat > 10:
            sys.exit('Host heartbeat expired or capture deadline exceeded.')
        if now >= next_guard:
            if active() != win or geometry() != size or identity() != ident:
                sys.exit('The visible foreground player changed during capture.')
            next_guard = now + 0.25
        if now >= next_audio:
            audio_guard()
            next_audio = now + 0.75
        if not select.select([0], [], [], 0.15)[0]:
            continue
        buf = os.read(0, 4096)
        if not buf:
            sys.exit('Host control pipe was closed.')
        pending += buf
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            if line == b'alive':
                last_heartbeat = time.monotonic()
            elif line == b'stop':
                stopped = True
                break
            else:
                sys.exit('Capture cancelled by host.')
        if stopped:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            code = proc.wait(timeout=8)
            if code not in (0, 255):
                sys.exit(f'ffmpeg stopped unexpectedly: {code}')
            print('WX_STOP=early', file=sys.stderr, flush=True)
            break
    if not stopped and proc.returncode != 0:
        sys.exit(f'ffmpeg failed with status {proc.returncode}')
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except sp.TimeoutExpired:
            proc.kill()
            proc.wait()
"""


def make_external_directory(path):
    # Start from the existing project root. Never recreate a missing /Volumes mount.
    current = BASE
    for part in path.relative_to(BASE).parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)


def guest_command(window, crop, duration, fps, mode):
    selector = 'auto' if window in (None, 'auto') else hex(int(str(window), 0))
    return DOCKER + ['exec', '-i', '-u', 'abc', CONTAINER, 'python3', '-c', GUEST, selector,
                     *map(str, crop), str(duration), str(fps), mode]


def preflight(window, crop):
    """Check the foreground player and audio routing without recording. Return its X11 id."""
    check_drive()
    p = subprocess.run(guest_command(window, crop, 1, 24, 'check'), input='', capture_output=True,
                       text=True, timeout=12)
    match = re.search(r'^WX_WINDOW=(\d+)$', p.stderr, re.M)
    if p.returncode or not match:
        raise RuntimeError(p.stderr.strip() or 'The capture window is unavailable.')
    return int(match[1])


def assert_quiet_before_playback():
    """With the intended work paused, sample the actual playback monitor for other audio."""
    import array
    check_drive()
    command = DOCKER + ['exec', '-u', 'abc', CONTAINER, 'ffmpeg', '-nostdin', '-hide_banner',
                        '-loglevel', 'error', '-f', 'pulse', '-sample_rate', '48000', '-channels', '2',
                        '-i', 'output.monitor', '-t', '0.7', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']
    data = subprocess.check_output(command, timeout=6)
    samples = array.array('h')
    samples.frombytes(data)
    if sys.byteorder != 'little':
        samples.byteswap()
    if len(samples) < 48000:
        raise RuntimeError('Cannot verify the idle playback monitor.')
    block = 4800  # 50 ms of stereo PCM
    rms = max(math.sqrt(sum(n*n for n in samples[i:i+block])/block)
              for i in range(0, len(samples)-block+1, block))
    peak = max(map(abs, samples))
    def decibels(value):
        return 20 * math.log10(max(value, 1)/32768)
    rms_db, peak_db = decibels(rms), decibels(peak)
    if rms_db > -49 or peak_db > -33:
        raise RuntimeError('Audio is playing while the target is paused. Close other playing tabs and retry.')
    return {'sampled_seconds': len(samples)/96000, 'peak_dbfs': round(peak_db, 1), 'loudest_50ms_rms_dbfs': round(rms_db, 1)}


def check_aac_clock(packets):
    """Audit 48 kHz AAC-LC frame starts in the recording's millisecond time base."""
    if len(packets) < 2:
        raise RuntimeError('Missing AAC packet timestamps.')
    repair = False
    prior = packets[0]
    for i in range(1, len(packets)):
        stamp = packets[i]
        gap = stamp - packets[i - 1]
        if gap < 0:
            raise RuntimeError(f'AAC timestamp reversal at packet {i}.')
        following = packets[i + 1] if i + 1 < len(packets) else None
        first = following is not None and 0 <= following - stamp <= 1
        second = gap <= 1
        if first or second:
            # A pair may land in the same or consecutive millisecond tick.
            # Require two unaffected neighbours spanning three AAC frames.
            if first and second or (second and (i < 2 or following is None)):
                raise RuntimeError(f'Unresolved AAC timestamp overlap at packet {i}.')
            if first:
                span = packets[i + 2] - packets[i - 1] if i + 2 < len(packets) else 0
                if not 60 <= span <= 68:
                    raise RuntimeError(f'Unresolved AAC timestamp overlap at packet {i}.')
                if not 19 <= gap <= 24:
                    stamp = packets[i - 1] + 1024 / 48
            elif not 19 <= following - stamp <= 24:
                stamp = following - 1024 / 48
            repair = True
        # One AAC-LC frame is 1024 samples. Never bridge a missing frame,
        # a substantial clock reversal, or accumulating audio sample loss.
        if not 10 <= stamp - prior <= 33 or abs((stamp - packets[0]) * 48 - i * 1024) > 2400:
            raise RuntimeError(f'AAC timestamp discontinuity at packet {i}.')
        prior = stamp
    return repair


def read_aac_packets(raw, timeout):
    data = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
        'packet=pts,dts:stream=codec_name,sample_rate,time_base', '-of', 'csv=p=1', str(raw),
    ], timeout=timeout)
    packets = array('q')
    stream = None
    for line in io.BytesIO(data):
        fields = line.strip().split(b',')
        if fields[0] == b'packet':
            if len(fields) < 3 or fields[1] != fields[2] or not re.fullmatch(rb'-?\d+', fields[1]):
                raise RuntimeError('Invalid AAC packet timestamp.')
            packets.append(int(fields[1]))
        elif fields[0] == b'stream':
            stream = fields[1:4]
    if stream != [b'aac', b'48000', b'1/1000']:
        raise RuntimeError('Expected 48 kHz AAC from the Matroska recorder.')
    return packets


def audio_remux_options(packets):
    if not check_aac_clock(packets):
        return []
    # Mirror the audit above in sample ticks. Place an early/late packet
    # exactly one AAC frame from its unaffected neighbour. Copy all others.
    first = 'between(NEXT_PTS-TS,0,48)*not(between(TS-PREV_INPTS,912,1152))'
    second = 'between(TS-PREV_INPTS,0,48)*not(between(NEXT_PTS-TS,912,1152))'
    fix = f'if({first},PREV_INPTS+1024,if({second},NEXT_PTS-1024,TS))'
    expr = f"setts=ts='if(between(N,1,{len(packets)-2}),{fix},TS)':time_base=1/48000:prescale=1"
    return ['-bsf:a', expr]


def pulse_overlap_windows(packets):
    """Recognize isolated Pulse clock compressions with intact surrounding media time."""
    groups = []
    for i in range(1, len(packets)):
        gap = packets[i] - packets[i-1]
        if gap < 0 or gap > 33:
            return []
        if gap <= 12:
            if not groups or i - groups[-1][-1] > 24:
                groups.append([])
            groups[-1].append(i)
    if not groups:
        return []
    windows = bounded_pulse_plan(packets, groups, anchor_clock=False)
    return windows or bounded_pulse_plan(packets, groups, anchor_clock=True)


def bounded_pulse_plan(packets, groups, *, anchor_clock):
    windows = []
    removed = 0
    for group in groups:
        if (group[0] - 144 < 1 or group[-1] + 144 >= len(packets) or group[-1] - group[0] > 24
                or sum(packets[i] == packets[i-1] for i in group) < 2):
            return []
        # Prefer a symmetric six-second window. Clock drift preceding a burst
        # can put that split outside the permitted sync error. Choose a shifted
        # window of the same length only if every original timestamp still fits.
        for lead in (144, 168, 120, 192, 96, 216, 72):
            start, end = group[0] - lead, group[-1] + 288 - lead
            if start < 1 or end >= len(packets) or (windows and start <= windows[-1][1]):
                continue
            excess = (end - start) * 1024 - (packets[end] - packets[start]) * 48
            if not 30 * 48 <= excess <= 125 * 48:
                continue
            if anchor_clock:
                # Long takes can have independently measurable oscillator skew
                # between bursts. Only absorb it in this existing repair window
                # if the whole preceding stable stretch supports one smooth rate.
                previous = windows[-1][1] if windows else 0
                samples = (start - previous) * 1024
                drift = samples - (packets[start] - packets[previous]) * 48
                if abs(drift) > 480 + samples * .0001:  # 10 ms rounding/jitter + 100 ppm
                    continue
                if any(abs((i-previous)*1024 - (packets[i]-packets[previous])*48
                           - drift*(i-previous)/(start-previous)) > 480
                       for i in range(previous, start)):
                    continue
                excess = end * 1024 - (packets[end] - packets[0]) * 48 - removed
                if not 30 * 48 <= excess <= 125 * 48:
                    continue
            if all(abs((packets[i] - packets[0]) * 48 - i * 1024 + removed
                       + excess * (i - start) / (end - start)) <= 75 * 48 for i in range(start, end)):
                windows.append((start, end, excess))
                removed += excess
                break
        else:
            return []
    if not windows or sum(w[2] for w in windows) > len(packets) * 1024 * .0025:
        return []
    # Bound the deviation between sample time and every original timestamp.
    # Inside a burst its duplicate stamps cannot all be correct. Spread the
    # measured difference over the neighbouring six seconds, then rejoin the
    # original clock. Stable sections retain the usual 50 ms bound.
    index = removed = 0
    for i, timestamp in enumerate(packets):
        if index < len(windows) and i >= windows[index][1]:
            removed += windows[index][2]
            index += 1
        correction, limit = removed, 50 * 48
        if index < len(windows) and i >= windows[index][0]:
            start, end, excess = windows[index]
            correction += excess * (i - start) / (end - start)
            limit = 75 * 48
        if abs((timestamp - packets[0]) * 48 - (i * 1024 - correction)) > limit:
            return []
    return windows


def remux_pulse_overlaps(raw, ready, packets, windows, timeout):
    """Preserve every video packet and join locally resampled PCM through lossless ALAC."""
    frames = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_frames', '-show_entries',
        'frame=pts,nb_samples:stream=channels', '-of', 'csv=p=1', str(raw)], timeout=timeout)
    stamps, channels = array('q'), None
    for line in io.BytesIO(frames):
        fields = line.rstrip().split(b',')
        if fields[0] == b'frame':
            if fields[2] != b'1024':
                raise RuntimeError('Unexpected decoded AAC frame size.')
            stamps.append(int(fields[1]))
        elif fields[0] == b'stream':
            channels = int(fields[1])
    priming = len(packets) - len(stamps)
    if channels not in (1, 2) or priming not in (0, 1) or stamps != packets[priming:]:
        raise RuntimeError('Decoded AAC frames do not match the original packet timeline.')
    align = 3 * channels
    ffmpeg = ['ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-xerror', '-err_detect', 'explode']
    pcm_format = ['-f', 's24le', '-ar', '48000', '-ac', str(channels)]
    with tempfile.TemporaryFile(dir=ready.parent) as source, tempfile.TemporaryFile(dir=ready.parent) as corrected:
        subprocess.run([*ffmpeg, '-i', str(raw), '-map', '0:a:0', '-af', 'asetpts=N/SR/TB',
                        *pcm_format, 'pipe:1'], stdout=source, check=True, timeout=timeout)
        if source.tell() != len(stamps) * 1024 * align:
            raise RuntimeError('AAC sample count changed during decoding.')
        source.seek(0)
        for left, right, excess in windows:
            start = (left - priming) * 1024 * align
            while source.tell() < start:
                block = source.read(min(1024 * 1024, start - source.tell()))
                if not block:
                    raise RuntimeError('The decoded audio ended before the verified repair window.')
                corrected.write(block)
            samples = (right - left) * 1024
            rate = round(48000 * samples / (samples - excess))
            if not 48000 < rate <= 49200:
                raise RuntimeError('Local audio rate correction exceeds 2.5%.')
            piece = subprocess.run([*ffmpeg, *pcm_format, '-i', 'pipe:0', '-af', f'asetrate={rate}',
                                    *pcm_format, 'pipe:1'], input=source.read(samples * align),
                                   stdout=subprocess.PIPE, check=True, timeout=timeout).stdout
            if abs(len(piece) / align - (samples - excess)) > 5:
                raise RuntimeError('Local audio resampling did not meet its measured duration.')
            corrected.write(piece)
        shutil.copyfileobj(source, corrected)
        expected = len(stamps) * 1024 - sum(w[2] for w in windows)
        if abs(corrected.tell() / align - expected) > len(windows) * 5:
            raise RuntimeError('Unexpected corrected audio sample count.')
        corrected.seek(0)
        subprocess.run([*ffmpeg, '-copyts', '-i', str(raw), *pcm_format, '-i', 'pipe:0',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy',
                        '-af', f'asetpts=N/SR/TB+({stamps[0]}/1000)/TB',
                        '-c:a', 'alac', '-sample_fmt', 's32p', '-movflags', '+faststart', str(ready)],
                       stdin=corrected, check=True, timeout=timeout)


class LiveCapture:
    """A bounded local capture. Host monotonic to media PTS mapping is approximate."""

    def __init__(self, *, window=None, crop, duration, output, fps=24):
        self.window = window
        self.crop = tuple(crop)
        self.duration = float(duration)
        self.fps = int(fps)
        if len(self.crop) != 4 or not all(isinstance(v, int) for v in self.crop):
            raise ValueError('crop must be four integer coordinates X,Y,W,H')
        if not 1 <= self.duration <= 14400 or not 5 <= self.fps <= 60:
            raise ValueError('duration must be 1..14400 seconds and fps 5..60')
        self.output = Path(output).expanduser().resolve()
        allowed = [BASE/'media', BASE/'tmp']
        if self.output.suffix.lower() != '.mp4' or not any(d.resolve() in self.output.parents for d in allowed):
            raise ValueError('output must be a new MP4 under the external media or tmp directory')
        self.origin = None
        self.video_wall_origin = None
        self.actual_window = None
        self._proc = None
        self._stage = None
        self._reader = self._beater = None
        self._control_lock = threading.Lock()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._shutdown = threading.Event()
        self._logs = deque(maxlen=12)
        self._early = False
        self._published = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cancel()

    def _control(self, message):
        with self._control_lock:
            try:
                self._proc.stdin.write(message + '\n')
                self._proc.stdin.flush()
            except (OSError, ValueError):
                pass

    def _heartbeat(self):
        while not self._shutdown.is_set() and self.running():
            self._control('alive')
            self._shutdown.wait(1)

    def _read_progress(self):
        frame = 0
        pts = None
        guest_wall = host_mono = video_origin = None
        expect_video_start = False
        try:
            for message in self._proc.stderr:
                key, sep, value = message.strip().partition('=')
                if key == 'WX_WINDOW':
                    self.actual_window = int(value)
                elif key == 'WX_GUEST_WALL':
                    # The first X11 packet has a guest CLOCK_REALTIME timestamp.
                    # Relate it to this host's monotonic clock using a local socket sample.
                    guest_wall = float(value)
                    host_mono = time.monotonic()
                elif message.startswith('Input #'):
                    expect_video_start = message.startswith('Input #0, x11grab,')
                elif expect_video_start and 'Duration:' in message:
                    stamp = re.search(r'\bstart: ([\d.]+)', message)
                    if stamp and guest_wall is not None:
                        delay = float(stamp[1]) - guest_wall
                        if 0 <= delay <= 12:
                            video_origin = host_mono + delay
                            self.video_wall_origin = float(stamp[1])
                    expect_video_start = False
                elif key == 'WX_STOP':
                    self._early = value == 'early'
                elif key == 'frame' and value.strip().isdigit():
                    frame = int(value)
                elif key == 'out_time_us':
                    pts = None if value == 'N/A' else int(value)/1e6
                elif key == 'progress' and self.origin is None and frame > 0 and pts is not None and pts > 0:
                    self.origin = video_origin if video_origin is not None else time.monotonic() - pts
                    self._started.set()
                elif sep == '' or message.lstrip().startswith('['):
                    self._logs.append(message.strip())
        finally:
            self._finished.set()
            self._started.set()

    def start(self):
        """Start and wait for real ffmpeg progress. Return estimated host monotonic PTS zero."""
        if self._proc is not None:
            raise RuntimeError('This capture was already started.')
        check_drive()
        if self.output.exists():
            raise FileExistsError(self.output)
        make_external_directory(self.output.parent)
        stage_root = self.output.parent if BASE/'tmp' in self.output.parents else BASE/'tmp'
        self._stage = tempfile.TemporaryDirectory(prefix='wechat-player-', dir=stage_root)
        self._raw = Path(self._stage.name)/'capture.mkv'
        try:
            with self._raw.open('xb') as recording:
                self._proc = subprocess.Popen(guest_command(self.window, self.crop, self.duration, self.fps, 'record'),
                                              stdin=subprocess.PIPE, stdout=recording, stderr=subprocess.PIPE,
                                              text=True, bufsize=1)
            self._reader = threading.Thread(target=self._read_progress, daemon=True)
            self._beater = threading.Thread(target=self._heartbeat, daemon=True)
            self._reader.start()
            self._beater.start()
            if not self._started.wait(timeout=15) or self.origin is None:
                raise RuntimeError('Capture did not start: ' + ' '.join(self._logs))
            return self.origin
        except BaseException:
            self.cancel()
            raise

    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def elapsed(self):
        if self.origin is None:
            raise RuntimeError('No media progress yet.')
        return max(0., time.monotonic()-self.origin)

    def finish(self, *, on_recorded=None):
        """Request an early, clean end and publish the validated untrimmed capture."""
        if self.running():
            self._control('stop')
        return self._complete(15, on_recorded=on_recorded)

    def wait(self):
        """Wait for the requested duration and publish the validated untrimmed capture."""
        timeout = max(15, self.duration+30-self.elapsed())
        return self._complete(timeout)

    def _complete(self, timeout, *, on_recorded=None):
        if self._published:
            return self.output
        if self._proc is None or self.origin is None:
            raise RuntimeError('The recorder is not ready.')
        try:
            code = self._proc.wait(timeout=timeout)
            self._reader.join(timeout=3)
            if code or not self._finished.is_set():
                raise RuntimeError('Capture failed: ' + ' '.join(self._logs))
            if on_recorded is not None:
                on_recorded(self._raw, self._early)
            ready = Path(self._stage.name)/'verified.mp4'
            self._validate(ready)
            check_drive()
            os.link(ready, self.output)
            self._published = True
            return self.output
        finally:
            self.cancel()

    def _validate(self, ready):
        self.validate_recorded(self._raw, ready, early=self._early)

    def validate_recorded(self, source, ready, *, early):
        """Strictly remux and decode a completed capture into a new temporary MP4."""
        limit = max(45, self.duration + 30)
        packets = read_aac_packets(source, limit)
        try:
            options = audio_remux_options(packets)
        except RuntimeError:
            windows = pulse_overlap_windows(packets)
            if not windows:
                raise
            remux_pulse_overlaps(source, ready, packets, windows, limit)
        else:
            subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-xerror','-i',str(source),
                            '-map','0:v:0','-map','0:a:0','-c','copy', *options,
                            '-movflags','+faststart',str(ready)], check=True, timeout=limit)
        details = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(ready)],
                                                     timeout=15))['streams']
        tracks = {s['codec_type']: s for s in details}
        if set(tracks) != {'audio','video'} or len(details) != 2:
            raise RuntimeError('Expected a video track and a programme audio track.')
        lengths = [float(s.get('duration', 0)) for s in details]
        if (not all(0 < sec <= self.duration+0.65 for sec in lengths)
                or max(lengths)-min(lengths)>0.5 or min(lengths)<0.45
                or (not early and any(abs(sec-self.duration) > 0.65 for sec in lengths))):
            raise RuntimeError(f'Capture duration mismatch: {lengths}')
        if (tracks['video'].get('width'), tracks['video'].get('height')) != self.crop[2:]:
            raise RuntimeError('Captured video dimensions changed.')
        probe = subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','info','-xerror','-i',str(ready),
                                '-map','0:v:0','-map','0:a:0','-af','volumedetect','-f','null','-'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               check=True, timeout=limit)
        match = re.search(r'max_volume:\s*([-\w.]+) dB', probe.stderr)
        if not match or not math.isfinite(float(match[1])) or float(match[1]) < -70:
            raise RuntimeError('The captured programme audio is silent.')

    def cancel(self):
        """Stop and discard an in-progress capture without replacing existing files."""
        self._shutdown.set()
        if self.running():
            self._control('cancel')
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=4)
        if self._proc is not None:
            self._proc.stdin.close()
            if self._reader is not None:
                self._reader.join(timeout=3)
            self._proc.stderr.close()
        if self._beater is not None:
            self._beater.join(timeout=2)
        if self._stage is not None:
            self._stage.cleanup()
            self._stage = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window', default='auto', help='foreground WeChat window id or auto')
    parser.add_argument('--crop', required=True, metavar='X,Y,W,H', help='picture relative to the window')
    parser.add_argument('--duration', type=float, default=10)
    parser.add_argument('--fps', type=int, default=24)
    parser.add_argument('--output', type=Path, help='new MP4 within external media/ or tmp/')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        crop = tuple(int(n) for n in args.crop.split(','))
        if len(crop) != 4:
            raise ValueError('Expected four crop coordinates.')
        if args.check:
            window = preflight(args.window, crop)
            print(f'Window {hex(window)}; selected {crop}; output.monitor available.')
            return 0
        if not args.output:
            parser.error('--output is required')
        with LiveCapture(window=args.window, crop=crop, duration=args.duration,
                         output=args.output, fps=args.fps) as take:
            start = take.start()
            print(f'RECORDING — host_monotonic_origin={start:.6f}', flush=True)
            print(f'Saved {take.wait()}', flush=True)
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc)+'\n')


if __name__ == '__main__':
    sys.exit(main())
