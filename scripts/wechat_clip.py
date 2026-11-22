#!/usr/bin/env python3
"""Trim reviewed playback timestamps (seconds, end exclusive) and validate the result."""
import argparse
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from wechat_paths import DATA_ROOT as BASE, check_drive
from wechat_preview import SHARE_URL
from wechat_recording import file_signature


def run(arguments, timeout):
    process = subprocess.run(arguments, capture_output=True, timeout=timeout,
                             env={**os.environ, "LC_ALL": "C"})
    if process.returncode:
        error = process.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"{arguments[0]} failed ({process.returncode}): {error[-1600:]}")
    return process


def probe(path):
    result = run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                  "-of", "json", str(path)], 45)
    data = json.loads(result.stdout)
    streams = {}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind in {"video", "audio"} and not stream.get("disposition", {}).get("attached_pic"):
            if kind in streams:
                raise ValueError(f"expected one {kind} stream")
            streams[kind] = stream
    if set(streams) != {"video", "audio"}:
        raise ValueError("expected one video stream and one audio stream")
    for stream in streams.values():
        length = float(stream.get("duration", "nan"))
        if not math.isfinite(length) or length <= 0:
            raise ValueError("media stream has no finite positive duration")
    return streams, data["format"]


def verify_clip(path, expected_duration=None):
    """Decode both streams to EOF. Return lengths, size, geometry and measured peak in dBFS."""
    path = Path(path).resolve(strict=True)
    streams, form = probe(path)
    video = streams["video"]
    audio = streams["audio"]
    vd, ad = float(video["duration"]), float(audio["duration"])
    try:
        rate = Fraction(video.get("avg_frame_rate", "0"))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("unknown frame rate") from exc
    if rate <= 0:
        raise ValueError("unknown frame rate")
    tolerance = max(0.10, min(0.50, 2 / float(rate)))
    if int(video.get("nb_frames", 0)) <= 0 or min(video["width"], video["height"], audio["channels"]) <= 0:
        raise ValueError("incomplete output streams")
    if abs(vd - ad) > tolerance:
        raise ValueError(f"audio/video duration mismatch ({ad} and {vd} seconds)")
    if expected_duration is not None:
        if not math.isfinite(expected_duration) or expected_duration <= 0:
            raise ValueError("expected duration must be finite and positive")
        if max(abs(vd - expected_duration), abs(ad - expected_duration)) > tolerance:
            raise ValueError("output duration differs from the requested time range")
    stats = run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-xerror",
                 "-err_detect", "explode", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0",
                 "-af", "volumedetect", "-f", "null", "-"], max(120, 60 + 4 * max(vd, ad)))
    match = re.search(rb"max_volume:\s*([-\d.]+) dB", stats.stderr)
    peak = float(match[1]) if match else -math.inf
    if not math.isfinite(peak) or peak < -70:
        raise ValueError("playback audio was silent or below -70 dBFS")
    return dict(path=str(path), size=path.stat().st_size, duration=float(form["duration"]),
                video_duration=vd, audio_duration=ad, peak_dbfs=peak,
                width=video["width"], height=video["height"], frames=int(video["nb_frames"]))


def clip_recording(source, output, *, start, end, audio_start=None, title="", author="", share_url=""):
    """Convert a confirmed WeChat capture. Video [start,end) and audio have equal requested length.

    Times are seconds on the input MP4 timeline, measured from zero. The caller confirms
    the work and the frame boundaries. Existing output with matching input hash, range
    and descriptive metadata is revalidated and reused. Other output is never replaced.
    """
    source = Path(source).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    check_drive(BASE)
    if BASE not in source.parents or BASE not in output.parents or source == output:
        raise ValueError("source and output must be distinct files under the external project directory")
    if output.suffix.lower() != ".mp4":
        raise ValueError("output must be an MP4")
    if share_url and not SHARE_URL.fullmatch(share_url):
        raise ValueError("expected a public WeChat Channels share URL")
    start, end = float(start), float(end)
    audio_start = start if audio_start is None else float(audio_start)
    if not all(map(math.isfinite, (start, end, audio_start))) or min(start, audio_start) < 0 or end <= start:
        raise ValueError("invalid clip boundaries")
    length = end - start
    audio_end = audio_start + length
    streams, source_format = probe(source)
    timeline_origin = float(source_format.get("start_time", 0))
    for kind, first, stop in (("video", start, end), ("audio", audio_start, audio_end)):
        stream = streams[kind]
        offset = float(stream.get("start_time", timeline_origin)) - timeline_origin
        if first < offset - 0.05:
            raise ValueError(f"requested start precedes the {kind} stream")
        if stop > offset + float(stream["duration"]) + 0.001:
            raise ValueError(f"requested end exceeds the {kind} stream")

    initial = source.stat()
    _, source_hash = file_signature(source)
    comment = "wechat-playback-clip-v1 " + json.dumps(
        dict(source_sha256=source_hash, start=start, end=end, audio_start=audio_start,
             title=title, author=author, share_url=share_url),
        ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def existing():
        _, form = probe(output)
        if form.get("tags", {}).get("comment") != comment:
            raise FileExistsError(f"a different file already exists: {output}")
        return dict(verify_clip(output, length), reused=True)

    if output.exists():
        return existing()
    parent = next(p for p in output.parents if p.exists())
    if parent.stat().st_dev != BASE.stat().st_dev or initial.st_dev != BASE.stat().st_dev:
        raise ValueError("source or output is on another filesystem")
    # On removal, never recreate the mount path on the host filesystem.
    directory = parent
    for part in output.parent.relative_to(parent).parts:
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wechat-clip-", dir=output.parent) as stage:
        temp = Path(stage) / "verified.mp4"
        filters = (f"[0:v:0]trim=start={start:.9f}:end={end:.9f},setpts=PTS-STARTPTS[v];"
                   f"[0:a:0]atrim=start={audio_start:.9f}:end={audio_end:.9f},asetpts=PTS-STARTPTS[a]")
        run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "error", "-xerror",
             "-err_detect", "explode", "-copyts", "-start_at_zero", "-i", str(source),
             "-filter_complex", filters,
             "-map", "[v]", "-map", "[a]", "-fps_mode:v", "vfr", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac",
             "-b:a", "160k", "-map_metadata", "-1", "-movflags", "+faststart",
             "-metadata", f"comment={comment}", "-metadata", f"title={title}",
             "-metadata", f"artist={author}", "-n", str(temp)], max(120, 90 + 6 * max(end, audio_end)))
        result = verify_clip(temp, length)
        current = source.stat()
        for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"):
            if getattr(initial, field) != getattr(current, field):
                raise OSError("source changed while preparing the clip")
        with temp.open("rb") as completed:
            os.fsync(completed.fileno())
        try:
            os.link(temp, output)
        except FileExistsError:
            return existing()
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return dict(result, path=str(output), reused=False)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("source", type=Path)
    cli.add_argument("output", type=Path)
    cli.add_argument("--start", required=True, type=float)
    cli.add_argument("--end", required=True, type=float)
    cli.add_argument("--audio-start", type=float)
    cli.add_argument("--title", default="")
    cli.add_argument("--author", default="")
    cli.add_argument("--share-url", default="")
    try:
        print(json.dumps(clip_recording(**vars(cli.parse_args())), ensure_ascii=False))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        cli.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
