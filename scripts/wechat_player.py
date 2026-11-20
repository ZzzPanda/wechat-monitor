#!/usr/bin/env python3
"""Control the visible, logged-in Linux WeChat Channels window using its ordinary UI."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import io
import json
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import unicodedata

import numpy as np
from PIL import Image

from wechat_paths import DATA_ROOT, DOCKER_HOST

VISION = DATA_ROOT / "runtime/bin/wechat-visible-text-stdin"
DOCKER = [
    "docker",
    "--host",
    DOCKER_HOST,
    "exec",
    "-u",
    "abc",
    "-e",
    "HOME=/config",
    "-e",
    "LC_ALL=C",
    "wx-playback",
]
SHARE = re.compile(r"https://weixin\.qq\.com/sph/[A-Za-z0-9]+")
CLOCK = re.compile(r"(?<!\d)(\d{1,3}):([0-5]\d)\s*/\s*(\d{1,3}):([0-5]\d)(?!\d)")


class PlayerError(RuntimeError):
    pass


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def caption_text(value):
    # The desktop client draws “展开” after the ellipsis; Vision may join both text boxes.
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"(?:[⋯….•·]+|\s+)\s*展开\s*$", "", text)
    # The standalone token AI sometimes comes back as Al/A1. Preserve words such as Alpha.
    text = re.sub(r"(?<![a-zA-Z0-9])A[lI1](?![a-zA-Z0-9])", "AI", text)
    return norm(text)


def player_caption(rows, width, author_row, caption):
    # Vision can split one rendered line at an emoji. Reassemble adjacent boxes
    # before comparing, and keep their full bounds for the playback observer.
    wanted = caption_text(caption)
    candidates = []
    labels = sorted((r for r in rows if author_row["y"] - 100 <= r["y"] <= author_row["y"] - 20),
                    key=lambda r: r["x"])
    for i, first in enumerate(labels):
        if first["x"] > .10 * width:
            break
        merged = first.copy()
        for following in labels[i+1:]:
            gap = following["x"] - (merged["x"] + merged["width"])
            if gap > max(24, first["height"]):
                break
            if gap < -2 or abs(center(first)[1] - center(following)[1]) > first["height"] * .35:
                continue
            merged["text"] += " " + following["text"]
            merged["width"] = following["x"] + following["width"] - first["x"]
            bottom = max(merged["y"] + merged["height"], following["y"] + following["height"])
            merged["y"] = min(merged["y"], following["y"])
            merged["height"] = bottom - merged["y"]
        for label in (merged, first):
            text = caption_text(label["text"])
            if "🆚" in caption:
                # The live desktop read a comparison between years as both 'u' and 'uS'.
                text = re.sub(r"(?<=\d)[uv]s?(?=\d)", "", text)
                if label is first and text.isdecimal() and text != wanted:
                    continue
            if len(text) >= min(8, len(wanted)) and (wanted.startswith(text) or text.startswith(wanted)):
                # Prefer the full line. The next box can also be a separate UI label.
                candidates.append(label)
                break
    return candidates[0] if len(candidates) == 1 else None


def author_matches(text, author):
    # Vision can join the account name, its verification badge and the follow status.
    name = unicodedata.normalize("NFKC", text).strip()
    name = re.sub(r"\s*已关注$", "", name).strip()
    if norm(name) == norm(author):
        return True
    parts = name.rsplit(None, 1)
    return len(parts) == 2 and len(parts[1]) == 1 and norm(parts[0]) == norm(author)


def author_profile(rows, author, size=None):
    # In the full-width profile the account header, post count and tabs share
    # the left column. An author sidebar renders those same labels on the right.
    left = (size[0] if size else 900) / 3
    labels = [(row["y"], norm(row["text"])) for row in rows if 0 <= row["x"] < left]
    author = norm(author)
    headers = [y for y, text in labels if text == "公众号" + author or (y > 60 and text == author)]
    counts = [y for y, text in labels if "原创内容" in text]
    tabs = [y for y, text in labels if text == "直播回放"]
    return any(header < count < tab for header in headers for count in counts for tab in tabs)


def runs(mask):
    edges = np.diff(np.r_[False, mask, False].astype("int8"))
    return list(zip(np.where(edges == 1)[0], np.where(edges == -1)[0]))


def run(args, **kwargs):
    return subprocess.run(
        args, check=True, capture_output=True, timeout=kwargs.pop("timeout", 8), **kwargs
    ).stdout


def lines(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return json.loads(run([str(VISION)], input=buf.getvalue()))["lines"]


def visible_clock_position(image, expected_duration):
    """Read the time label from an already sampled frame of the verified work."""
    try:
        rows = lines(image.resize((image.width * 2, image.height * 2)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    clocks = [m for row in rows for m in CLOCK.finditer(
        unicodedata.normalize("NFKC", row["text"]))]
    if len(clocks) != 1:
        return None
    match = clocks[0]
    position = int(match[1]) * 60 + int(match[2])
    duration = int(match[3]) * 60 + int(match[4])
    if duration == expected_duration and position <= duration:
        return position
    return None


def opening_handle(image):
    """Find the moving handle in the first segment of a track over a black gutter."""
    a = np.asarray(image.convert("L"))
    track_rows = (a[:, 30:-4] >= 38).mean(axis=1) >= .97
    positions = []
    for top, bottom in runs(track_rows):
        if not 2 <= bottom - top <= 8 or top < 5:
            continue
        y = (top + bottom - 1) // 2
        lobes = [(x, end) for x, end in runs(a[y-5] > 165) if 6 <= end-x <= 18]
        if len(lobes) == 1:
            x, end = lobes[0]
            positions.append((x + end - 1) / 2 - 12)
    if len(positions) == 1 and positions[0] >= -3:
        return max(0., positions[0])
    return None


def opening_stamp(samples, clicked, seconds_per_pixel):
    # A displayed 0:00 alone can last through buffering. Require a sample at the
    # actual origin followed by multiple advancing positions at playback speed.
    origin_sample = None
    moving = []
    for stamp, pixels in samples:
        if stamp < clicked or pixels is None:
            continue
        position = pixels * seconds_per_pixel
        if position <= .10:
            origin_sample, moving = stamp, []
        elif origin_sample is not None and .10 < position <= 2.0:
            if moving and position < moving[-1][1] - .04:
                return None
            moving.append((stamp, position))
            if len(moving) >= 3 and position - moving[0][1] >= .22:
                origins = [t - p for t, p in moving]
                if (max(origins) - min(origins) <= .18 and
                        abs(min(origins) - origin_sample) <= .25):
                    # Include one preceding verification frame to preserve the onset.
                    return max(clicked, min(origins) - .125)
    return None


def center(row):
    return row["x"] + row["width"] // 2, row["y"] + row["height"] // 2


def guest(*args):
    return run([*DOCKER, *map(str, args)])


class Player:
    def __init__(self, author, caption, duration=None):
        self.author = str(author)
        self.caption = str(caption).strip()
        self.duration = int(duration) if duration else None
        if not norm(self.author) or len(norm(self.caption)) < 5:
            raise PlayerError(
                "Author and at least five characters of the public caption are required."
            )
        self.window = None
        self.size = None
        self.crop = None
        self.controls = None
        self.title = None
        self.name = None
        self.reference = None
        self.seek_origin = None
        self.start = None

    def _check(self):
        data = guest(
            "sh",
            "-c",
            """
            active=$(xdotool getactivewindow) || exit 1
            printf '%s\n' "$active"
            xprop -id "$active" WM_CLASS || exit 1
            xwininfo -id "$active" || exit 1
        """,
        ).decode()
        active = int(data.splitlines()[0])
        if self.window is not None and active != self.window:
            raise PlayerError("The confirmed WeChat window lost focus.")
        if '"WeChatAppEx"' not in data:
            raise PlayerError(
                "Bring the WeChat Channels player or author's Channels page to the foreground."
            )
        if "IsViewable" not in data:
            raise PlayerError("The Channels window is hidden.")
        size = tuple(int(re.search(rf"\b{key}:\s*(\d+)", data)[1]) for key in ("Width", "Height"))
        if self.size and size != self.size:
            raise PlayerError("The Channels window was resized.")
        if not (900 <= size[0] <= 2000 and 650 <= size[1] <= 1400):
            raise PlayerError("Unsupported Channels window size.")
        self.window, self.size = active, size

    def _image(self, box=None):
        self._check()
        x, y, w, h = box or (0, 0, *self.size)
        before = time.monotonic()
        data = guest(
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "x11grab",
            "-framerate",
            "20",
            "-probesize",
            "32",
            "-analyzeduration",
            "0",
            "-window_id",
            str(self.window),
            "-grab_x",
            str(x),
            "-grab_y",
            str(y),
            "-video_size",
            f"{w}x{h}",
            "-draw_mouse",
            "0",
            "-i",
            ":1",
            "-frames:v",
            "1",
            "-c:v",
            "png",
            "-f",
            "image2pipe",
            "pipe:1",
        )
        self._check()
        im = Image.open(io.BytesIO(data)).convert("RGB")
        return before, im

    def _point(self, x, y, click=False):
        self._check()
        w, h = self.size
        if not (0 <= x < w and 0 <= y < h):
            raise PlayerError("Action fell outside the Channels window.")
        # Verify the active window inside the same Docker exec that delivers the input.
        cmd = '[ "$(xdotool getactivewindow)" = "$1" ] && xdotool mousemove --window "$1" "$2" "$3"'
        if click:
            cmd += " click 1"
        guest("sh", "-c", cmd, "sh", self.window, int(x), int(y))

    def _identity(self, rows):
        w, h = self.size
        # The account name and caption are drawn below the picture at distinct positions.
        names = [
            r
            for r in rows
            if 0.038 * w < r["x"] < 0.23 * w
            and r["y"] > 0.78 * h
            and author_matches(r["text"], self.author)
        ]
        for name in names:
            title = player_caption(rows, w, name, self.caption)
            if title:
                return title, name
        return None

    def _open_card(self, rows):
        if not author_profile(rows, self.author, self.size):
            raise PlayerError(
                "Open the specified author's Channels page with the target card in view, or open the work itself."
            )
        wanted = caption_text(self.caption)
        candidates = [
            r
            for r in rows
            if r["y"] > 160
            and len(caption_text(r["text"])) >= min(7, len(wanted))
            and wanted.startswith(caption_text(r["text"]))
        ]
        if len(candidates) != 1:
            raise PlayerError(
                "The visible author list has no unique matching card. Open the work directly."
            )
        self._point(*center(candidates[0]), click=True)

    def _sampled_footer_matches(self, image, left, top):
        # Only read the native footer of this timestamped frame. The picture
        # and its subtitles can contain unrelated account names and titles.
        x, y, w, h = self.identity_box
        x0, y0 = max(0, x-left-12), max(0, y-top-12)
        footer = image.crop((x0, y0, min(image.width, x-left+w+12), min(image.height, y-top+h+12)))
        # Fine glyphs and punctuation over moving footage sometimes resolve
        # only at a larger scale. Both reads use the very same source frame.
        for scale in (1, 2):
            sample = footer if scale == 1 else footer.resize((footer.width * scale, footer.height * scale))
            rows = [dict(row, x=left+x0+row['x']//scale, y=top+y0+row['y']//scale,
                         width=row['width']//scale, height=row['height']//scale) for row in lines(sample)]
            pair = self._identity(rows)
            if pair is not None and all(
                    abs(row['x'] - initial['x']) <= 12 and abs(row['y'] - initial['y']) <= 12
                    for row, initial in zip(pair, (self.title, self.name))):
                return True
        return False

    def _inspect(self, allow_open=False):
        deadline = time.monotonic() + 2
        while True:
            _, im = self._image()
            rows = lines(im)
            pair = self._identity(rows)
            if not pair:
                # Over moving footage Vision sometimes reads a footer only in
                # isolation. Keep coordinates on the original frame.
                top = int(im.height * .68)
                footer = lines(im.crop((0, top, im.width, im.height)))
                pair = self._identity([dict(row, y=row["y"] + top) for row in footer])
            if pair:
                break
            if allow_open and author_profile(rows, self.author, self.size):
                self._open_card(rows)
                allow_open = False
                deadline = time.monotonic() + 8
            if time.monotonic() >= deadline:
                raise PlayerError("The visible author and caption do not both match this work.")
            time.sleep(.12)
        title, name = pair
        self.title = title
        self.name = name
        x = min(title["x"], name["x"])
        y = min(title["y"], name["y"])
        right = max(row["x"] + row["width"] for row in (title, name))
        bottom = max(row["y"] + row["height"] for row in (title, name))
        self.identity_box = (x, y, right - x, bottom - y)
        ink = np.asarray(im.crop((x, y, right, bottom)).convert("L")) > 70
        self.identity_regions = []
        self.reference = []
        for row in (title, name):
            top = row["y"] - y
            left = row["x"] - x
            width = row["width"]
            if self._has_pillarbox():
                # On portrait works the footer overlays moving pixels. Compare only the
                # portion printed in the observed black pillarbox of this window.
                width = min(width, self.crop[0] - 3 - row["x"])
                if width < min(55, row["width"]):
                    raise PlayerError("Not enough stable text outside the portrait picture.")
            region = (slice(top, top + row["height"]), slice(left, left + width))
            self.identity_regions.append(region)
            self.reference.append(ink[region])
        return im

    def _toolbar(self):
        # Hover between known caption and picture. Derive buttons from the rendered time text and seek track.
        tx, ty = self.title["x"], self.title["y"]
        # The raised toolbar can move with pointer/letterbox state.
        top = max(55, ty - 245)
        trailer = (0, top, self.size[0], ty + 10 - top)
        portrait = self._has_pillarbox()

        def clock_labels(image, x_offset=0, y_offset=0):
            labels = []
            for row in lines(image):
                x, y = row["x"] + x_offset, row["y"] + y_offset
                if not (64 <= x < .20 * self.size[0] and 0 <= y < trailer[3]):
                    continue
                for match in CLOCK.finditer(unicodedata.normalize("NFKC", row["text"])):
                    labels.append((dict(row, x=x, y=y), match))
            return labels

        deadline = time.monotonic() + 4
        move = False
        while True:
            # While a work loads, WeChat can display 0:00 / 0:00 for several frames.
            if portrait and move:
                self._point(self.crop[0] + 24, ty - 110)
            else:
                self._point(tx + 108 + 6 * move, ty - 62 - 3 * move)
            move = not move
            _, im = self._image(trailer)
            # Vision sometimes merges the native play triangle into the left edge
            # of the clock box. Re-read the digits beyond the icon's right edge,
            # keeping their window-relative x for subsequent button recognition.
            for inset in (0, 68, 65, 70):
                region = im.crop((inset, 0, im.width // 4, im.height))
                clocks = clock_labels(region, x_offset=inset)
                if len(clocks) == 1:
                    break
            if len(clocks) != 1:
                # Vision can identify the clock in context even when every
                # narrow crop loses it. Validate controls on that same frame.
                _, window = self._image()
                clocks = clock_labels(window, y_offset=-top)
                im = window.crop((0, top, window.width, top + trailer[3]))
            if len(clocks) == 1:
                row, m = clocks[0]
                position = int(m[1]) * 60 + int(m[2])
                duration = int(m[3]) * 60 + int(m[4])
                if duration and position <= duration:
                    break
            if time.monotonic() >= deadline:
                raise PlayerError("Cannot read a ready, unambiguous player clock.")
            time.sleep(0.15)
        if self.duration and self.duration != duration:
            raise PlayerError(
                f"Player duration {duration}s differs from expected {self.duration}s."
            )
        if not 1 <= duration <= 4 * 3600:
            raise PlayerError("Invalid player duration.")
        mono = np.asarray(im.convert("L"))
        w, h = im.size
        cy = center(row)[1]
        possible = []
        for yy in range(max(0, cy - 8), min(h, cy + 9)):
            for left, right in runs(mono[yy] >= 50):
                if (
                    row["x"] + row["width"] < left < w * 0.40
                    and right - left > w * 0.34
                    and right < w * 0.90
                    # The native track crosses both black pillarboxes. On a bright
                    # portrait frame its image edges alone also pass the length test.
                    and (not portrait or (
                        left < self.crop[0] - 10 and right > self.crop[0] + self.crop[2] + 10))
                ):
                    possible.append((yy, left, right))
        if portrait and possible:
            rows = np.zeros(h, dtype=bool)
            rows[[t[0] for t in possible]] = True
            stripes = [(start, end) for start, end in runs(rows) if 2 <= end - start <= 8]
            if len(stripes) == 1:
                start, end = stripes[0]
                possible = [t for t in possible if start <= t[0] < end]
                if np.ptp([t[1] for t in possible]) > 4 or np.ptp([t[2] for t in possible]) > 4:
                    possible = []
            else:
                possible = []
        if not possible:
            raise PlayerError("The playback track is not recognizable.")
        y, left, right = map(int, np.median(possible, axis=0))
        # At the center of the pause icon there are two disjoint bars. Play is one contiguous triangle.
        left_icon = max(0, row["x"] - 52)
        right_icon = row["x"] - 5
        bars = [
            (a + left_icon, b + left_icon)
            for a, b in runs(mono[y, left_icon:right_icon] > 85)
            if 2 <= b - a <= 25
        ]
        if len(bars) not in (1, 2):
            raise PlayerError("The play/pause button is not recognizable.")
        paused = len(bars) == 1
        button = (int((bars[0][0] + bars[-1][1]) // 2), y + trailer[1])
        # The handle extends beyond the underlying bar at 0. Derive left edge at nonzero if needed.
        if position > max(2, duration * 0.02):
            self.seek_origin = left
        origin = self.seek_origin if self.seek_origin is not None else left
        if position == 0 and self.seek_origin is None:
            # The rounded handle protrudes to the left only at the true start.
            # A displayed 0:00 may already be several pixels into a short work.
            cap = mono[y - 5, left - 2:left + 22] > 160
            lobes = [(a, b) for a, b in runs(cap) if a <= 5 and 6 <= b - a <= 18]
            if len(lobes) == 1:
                a, b = lobes[0]
                origin = left - 2 + int((a + b - 1) // 2)
        self.controls = {"button": button, "seek": (origin + 1, y + trailer[1]),
                         "right": right, "clock_left": row["x"]}
        self.duration = duration
        return {"duration": duration, "position": position, "paused": paused}

    def _portrait_crop(self, luma):
        # On a portrait film the same two pillarbox edges recur across independent rows.
        # Require support at both ends of the picture as well as its measured 9:16 aspect.
        h, w = luma.shape
        y0, y1 = int(.18 * h), int(.74 * h)
        inset = max(8, w // 20)  # exclude window chrome at the outer edge
        mask = luma[y0:y1, inset:w-inset] > 10
        first = inset + mask.argmax(axis=1)
        last = w - inset - mask[:, ::-1].argmax(axis=1)
        valid = ((first > .12 * w) & (last < .88 * w) & (last > first)
                 & (np.abs(first + last - w) <= 6))
        if valid.sum() < len(mask) // 4:
            return None
        pairs, counts = np.unique(np.c_[first[valid], last[valid]], axis=0, return_counts=True)
        left, right = map(int, pairs[counts.argmax()])
        aligned = valid & (np.abs(first - left) <= 1) & (np.abs(last - right) <= 1)
        if sum(part.mean() >= .2 for part in np.array_split(aligned, 4)) < 3:
            return None
        width = right - left
        inside = (luma[:, left:right] > 10).mean(axis=1)
        left_pad = (luma[:, left-14:left-2] > 10).mean(axis=1)
        right_pad = (luma[:, right+2:right+14] > 10).mean(axis=1)
        picture_rows = (inside > .24) & (left_pad < .08) & (right_pad < .08)
        if picture_rows[-4:].mean() < .75:
            return None
        for top, end in runs(picture_rows):
            if not (.04 * h <= top < .24 * h and end - top >= 4):
                continue
            if abs((h - top) - width * 16 / 9) > 2.5:
                continue
            if picture_rows[top:top+6].mean() < .80 or picture_rows[top:].mean() < .50:
                continue
            height = (h - int(top)) // 2 * 2
            return left, h-height, width // 2 * 2, height
        return None

    def _get_crop(self, im):
        a = np.asarray(im)
        w, h = im.size
        portrait = self._portrait_crop(a.max(axis=2))
        if portrait:
            return portrait
        inset = max(20, w // 12)
        active = (a[:, inset : w - inset].max(axis=2) > 8).mean(axis=1) > 0.14
        groups = runs(active)
        # A title card or dark frame can split the picture in two; only observed outer edges define it.
        groups = [(s, e) for s, e in groups if s >= 50 and e < self.title["y"] - 30 and e - s > 3]
        for s, _ in groups:
            for _, e in reversed(groups):
                if (
                    0.552 < (e - s) / w < 0.573
                    and s < 0.27 * h
                    and e > 0.50 * h
                    and active[max(s, 0) : e].mean() > 0.53
                ):
                    cw = (w // 16) * 16
                    ch = cw * 9 // 16
                    if ch % 2:
                        cw -= 16
                        ch = cw * 9 // 16
                    return ((w - cw) // 2, int(s + (e - s - ch) // 2), cw, ch)
        raise PlayerError(
            "Visible edges do not confirm a supported 16:9 or pillarboxed 9:16 picture."
        )

    def _confirm_crop(self, im):
        for attempt in range(6):
            try:
                return self._get_crop(im)
            except PlayerError:
                if attempt == 5:
                    # _inspect has verified the window, author and caption for
                    # this exact frame. Keep the full playback area for square
                    # or unusual ratios and for pictures with indistinct edges.
                    if self.window is None or self.size != im.size or not self.name or not self.title:
                        raise
                    w, h = im.size
                    top = 54  # The native Linux tab strip occupies rows 0..52.
                    return (0, top, w // 2 * 2, (h - top) // 2 * 2)
                time.sleep(0.18)
                im = self._inspect()

    def _has_pillarbox(self):
        # A tall fallback viewport need not contain black side bars. Only the
        # edge-detected portrait crop supplies a measured left/right gutter.
        return self.crop is not None and self.crop[0] > 0 and self.crop[3] > self.crop[2]

    def inspect(self):
        im = self._inspect()
        self.crop = self._confirm_crop(im)
        state = self._toolbar()
        return {
            **state,
            "window": self.window,
            "crop": self.crop,
            "author": self.author,
            "visible_caption": self.title["text"],
        }

    def _pause(self):
        for _ in range(4):
            state = self._toolbar()
            if state["paused"]:
                return state
            self._point(*self.controls["button"], click=True)
            time.sleep(0.18)
        state = self._toolbar()
        if state["paused"]:
            return state
        raise PlayerError("Unable to confirm a paused player.")

    def prepare(self):
        im = self._inspect(allow_open=True)
        self.crop = self._confirm_crop(im)
        self._pause()
        # Seeking through the visible track normally starts playback, even if previously paused.
        self._point(*self.controls["seek"], click=True)
        state = self._pause()
        if state["position"] > 2:
            raise PlayerError(f"Unable to confirm the opening frame (clock {state['position']}s).")
        self._inspect()
        return {
            **state,
            "window": self.window,
            "crop": self.crop,
            "author": self.author,
            "visible_caption": self.title["text"],
        }

    def copy_share_url(self):
        im = self._inspect()
        if not self._toolbar()["paused"]:
            raise PlayerError("Pause before copying the official work link.")
        return self.copy_visible_share_url(im, self.name)

    def copy_visible_share_url(self, im, author_row):
        """Use the ordinary Share button next to the already verified author footer."""
        templ = np.asarray(Image.open(Path(__file__).with_name("wechat_share_button.png")).convert("L")) > 95
        expected = (self.size[0] - 223, author_row["y"] - 29)
        plane = np.asarray(im.convert("L")) > 95
        matches = []
        for dx in range(-10, 11, 2):
            for dy in range(-10, 11):
                x, y = expected[0] + dx, expected[1] + dy
                part = plane[y : y + templ.shape[0], x : x + templ.shape[1]]
                if part.shape == templ.shape:
                    score = (part & templ).sum() / max(1, (part | templ).sum())
                    matches.append((score, x, y))
        score, x, y = max(matches)
        if score < 0.67:
            raise PlayerError("The ordinary Share arrow could not be verified.")
        self._point(x + 20, y + 22, click=True)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            _, menu = self._image()
            choices = [r for r in lines(menu) if r["text"].strip() == "复制链接"]
            if len(choices) == 1:
                break
        else:
            raise PlayerError("The Share menu has no unique Copy Link item.")
        sentinel = "wechat-player-pending-" + secrets.token_hex(12)
        run([*DOCKER, "xsel", "--clipboard", "--input"], input=sentinel.encode())
        self._point(*center(choices[0]), click=True)
        for _ in range(10):
            self._check()
            data = guest("xsel", "--clipboard", "--output").decode(errors="replace").strip()
            if SHARE.fullmatch(data):
                return data
            time.sleep(0.12)
        raise PlayerError("WeChat did not update its copied share link.")

    def _start_playback(self, state):
        for attempt in range(3):
            # Seeking from the prepared opening can start, pause or advance
            # playback. Use its actual play button when it is already ready.
            control = "button" if state["paused"] and state["position"] <= 1 else "seek"
            stamp = time.monotonic()
            self._point(*self.controls[control], click=True)
            state = self._toolbar()
            if control == "button" and state["paused"] and state["position"] <= 1:
                # The first sampled frame can still show the old paused icon.
                time.sleep(.15)
                state = self._toolbar()
            if not state["paused"] and state["position"] <= max(3, time.monotonic() - stamp + 1):
                return stamp
            if attempt < 2:
                self._inspect()
        raise PlayerError(f"无法确认从片头开始播放，最后读到 {state['position']} 秒"
                          f"（{'暂停' if state['paused'] else '播放中'}）。")

    def restart(self):
        self._inspect()
        state = self._toolbar()
        if not state["paused"] or state["position"] > 2:
            raise PlayerError("Prepare the paused work at its start first.")
        lx, cy = self.controls["seek"]
        lx -= 1
        width = self.controls["right"] - lx - 1
        # A short portrait work has a measured black gutter to the left of the
        # picture. It exposes the first portion of the real seek track and knob.
        if self.duration > 60 or not self.crop or self.crop[3] <= self.crop[2] or self.crop[0] - lx < 55:
            self.start = self._start_playback(state)
            return self.start
        from wechat_frames import FrameStream
        top = max(55, cy - 124)
        right = min(self.crop[0] - 3, lx + int(width * .20))
        box = (lx - 12, top, right - lx + 12, cy + 16 - top)
        samples = []
        stop = threading.Event()
        with FrameStream(self.window, self.size, box, fps=12) as stream, ThreadPoolExecutor(max_workers=1) as reader:
            def observe():
                while not stop.is_set():
                    stamp, image = stream.read(timeout=2)
                    samples.append((stamp, opening_handle(image)))

            observation = reader.submit(observe)
            try:
                clicked = self._start_playback(state)
                self.start = clicked
                deadline = time.monotonic() + 1.25
                while time.monotonic() < deadline:
                    if observation.done():
                        observation.result()
                    measured = opening_stamp(samples.copy(), clicked, self.duration / width)
                    if measured is not None:
                        self.start = measured
                        break
                    stop.wait(.04)
            finally:
                stop.set()
            observation.result()
        return self.start

    def watch(self, *, interrupt_guard=nullcontext):
        if self.start is None or self.reference is None:
            raise PlayerError("Call restart() on this Player before watch().")
        from wechat_frames import FrameStream

        tx, ty, tw, th = self.identity_box
        lx, cy = self.controls["seek"]
        lx -= 1
        rx = self.controls["right"]
        clock_left = self.controls.get("clock_left")
        left = max(0, min(tx - 12, lx - 12, clock_left - 7 if clock_left is not None else tx))
        top = max(55, min(ty - 220, cy - 130))
        right = min(self.size[0], max(tx + tw + 12, rx + 12))
        bottom = min(self.size[1], max(ty + th + 12, cy + 16))
        box = (left, top, right - left, bottom - top)
        recent_pos = 0
        last_good = changed = tail_clock = None
        last_footer_read = None
        # A solid, bright background makes binary overlap a poor identifier.
        # Such a template may match an unrelated bright scene after autoplay.
        use_pixels = self.name is None or all(ref.mean() < .75 for ref in self.reference)
        start_wall = None
        clock_future = clock_stamp = clock_source = None
        coarse_slider = self.duration > rx - lx - 1
        last_hover = 0
        hover_side = False
        # On a short portrait clip the controls overlay the film itself. Wake them
        # only long enough to see the final two seconds and identify the transition.
        tail_hover_seconds = min(12.0, max(4.0, self.duration * .10))
        with (ThreadPoolExecutor(max_workers=1) as reader,
              FrameStream(self.window, self.size, box, fps=8) as stream, interrupt_guard()):
            while time.monotonic() < self.start + self.duration + max(45, self.duration * 0.25):
                now = time.monotonic()
                # The clock vanishes on idle. Wake its normal hover region near the end.
                if (
                    now - self.start >= self.duration - tail_hover_seconds
                    and now - last_hover > 0.85
                    and changed is None
                ):
                    hover_side = not hover_side
                    # Hover inside the picture. In this client the clock can move
                    # up into the film when the lower letterbox is empty.
                    self._point(self.title["x"] + 110 + 16 * int(hover_side), cy - 145)
                    last_hover = time.monotonic()
                stamp, img = stream.read(timeout=2.0)
                if clock_future is not None and clock_future.done():
                    position = clock_future.result()
                    clock_future = None
                    if position is not None:
                        if recent_pos >= self.duration - 2 and position < self.duration * .2:
                            raise PlayerError("The clock reset without a confirmed programme change.")
                        recent_pos = max(recent_pos, position)
                        if position >= self.duration - 2 and (tail_clock is None or clock_stamp >= tail_clock):
                            tail_clock, clock_source = clock_stamp, "visible-clock"
                luma = np.asarray(img.convert("L"))
                mat = luma[ty - top : ty - top + th, tx - left : tx - left + tw] > 70
                matches = [
                    (mat[region] & ref).sum() / max(1, (mat[region] | ref).sum())
                    for region, ref in zip(self.identity_regions, self.reference)
                ]
                same_work = use_pixels and min(matches) >= .80
                if not same_work and self.name is not None:
                    # Moving footage behind the native labels changes many
                    # pixels without changing the title or account. Sample
                    # text periodically in the middle, and every frame at the
                    # tail so the transition bounds use real observations.
                    if (last_footer_read is not None and changed is None
                            and stamp - self.start < self.duration - 14 and stamp - last_footer_read < .8):
                        continue
                    same_work = self._sampled_footer_matches(img, left, top)
                    last_footer_read = stamp
                if same_work:
                    if last_good is None and hasattr(stamp, 'guest_wall'):
                        if not 0 <= stamp-self.start <= 6:
                            raise PlayerError("No early X11 clock observation near the confirmed playback start.")
                        start_wall = stamp.guest_wall - (stamp-self.start)
                    last_good = stamp
                    changed = None
                    if stamp - self.start < self.duration - 14:
                        continue
                    # The round seek handle extends above the track; its center encodes
                    # displayed progress without waiting for OCR or blocking frame sampling.
                    positions = []
                    region = luma[max(0, cy - top - 118) : cy - top + 17]
                    central = region[:, lx - left + 16 : rx - left - 100]
                    bright = (central >= 235).mean(axis=1) > 0.97
                    for row0, row1 in runs(bright):
                        if not 2 <= row1 - row0 <= 8:
                            continue
                        yy = max(0, cy - top - 118) + (row0 + row1 - 1) // 2
                        strip = luma[yy - 5, lx - left - 9 : rx - left + 10]
                        blobs = [(a, b) for a, b in runs(strip > 175) if 6 <= b - a <= 18]
                        track = luma[yy, lx - left + 10 : rx - left - 10]
                        if len(blobs) != 1 or (track >= 38).mean() <= 0.96:
                            continue
                        a, b = blobs[0]
                        center = lx - 9 + (a + b - 1) / 2
                        pct = max(0.0, min(1.0, (center - lx) / (rx - lx - 1)))
                        positions.append((round(pct * self.duration), yy))
                    if len(positions) == 1:
                        position, _ = positions[0]
                        if recent_pos >= self.duration - 2 and position < self.duration * 0.2:
                            raise PlayerError(
                                "The progress bar reset without a confirmed programme change."
                            )
                        if not coarse_slider:
                            recent_pos = max(recent_pos, position)
                            if position >= self.duration - 2:
                                tail_clock, clock_source = stamp, "progress-bar"
                    # Long works advance by multiple seconds per slider pixel. Read the
                    # printed clock on a worker using this identity-matched image and
                    # retain its acquisition time even if OCR completes after autoplay.
                    if (clock_left is not None and stamp - self.start >= self.duration - tail_hover_seconds
                            and clock_future is None and (clock_stamp is None or stamp - clock_stamp >= .40)):
                        y0, y1 = max(0, cy - top - 130), cy - top + 18
                        if len(positions) == 1:
                            _, yy = positions[0]
                            y0, y1 = max(0, yy - 22), min(img.height, yy + 23)
                        label = img.crop((clock_left - left - 7, y0, lx - left + 8, y1))
                        clock_future = reader.submit(visible_clock_position, label, self.duration)
                        clock_stamp = stamp
                else:
                    if last_good is None:
                        raise PlayerError("Wrong programme when monitoring began.")
                    if changed is None:
                        changed = stamp
                    if stamp - changed >= 0.36:
                        if tail_clock is None and clock_future is not None and stamp - changed < 2:
                            continue
                        age = None if tail_clock is None else round(changed - tail_clock, 3)
                        gap = changed - last_good
                        if (
                            recent_pos < self.duration - 2
                            or age is None
                            or age > 3.5
                            or gap > 0.65
                            or last_good - self.start < self.duration - 2.5
                        ):
                            raise PlayerError(
                                f"Uncertain end: progress {recent_pos}/{self.duration}, "
                                f"frame gap {gap:.3f}s, near-end clock age {age}, "
                                f"elapsed {changed - self.start:.2f}s, match {matches}."
                            )
                        return {
                            "end_monotonic": last_good - 0.25,
                            "first_change_monotonic": changed,
                            **({"start_guest_wall": start_wall, "end_guest_wall": last_good.guest_wall - .25,
                                "first_change_guest_wall": changed.guest_wall}
                               if start_wall is not None else {}),
                            "last_position": int(recent_pos),
                            "position_source": clock_source,
                            "duration": self.duration,
                        }
        raise PlayerError(
            f"Playback timed out. Last confirmed progress {recent_pos}/{self.duration}."
        )

    def return_to_author(self):
        """Return via the ordinary account-name link only if this same author is still displayed."""
        self._check()
        _, im = self._image()
        records = lines(im)
        author = norm(self.author)

        for _ in range(6):
            if author_profile(records, self.author, self.size):
                return True
            w, h = self.size
            same_author = [
                r
                for r in records
                if 0.038 * w < r["x"] < 0.23 * w
                and r["y"] > 0.78 * h
                and author_matches(r["text"], self.author)
            ]
            if len(same_author) != 1:
                raise PlayerError("Open the target author's Channels profile.")
            # Closing the visible, confirmed programme also closes its audio. If another
            # of this author's programme tabs was underneath, close it only after verification.
            guest(
                "sh",
                "-c",
                '[ "$(xdotool getactivewindow)" = "$1" ] && xdotool key --clearmodifiers ctrl+w',
                "sh",
                self.window,
            )
            time.sleep(0.22)
            _, page = self._image()
            records = lines(page)
            if author_profile(records, self.author, self.size):
                return True
            tabs = [
                r
                for r in records
                if r["y"] < 45 and len(norm(r["text"])) >= 4 and author.startswith(norm(r["text"]))
            ]
            if len(tabs) == 1:
                self._point(*center(tabs[0]), click=True)
                for _ in range(4):
                    _, page = self._image()
                    if author_profile(lines(page), self.author, self.size):
                        return True
                raise PlayerError("The visible author tab did not open its profile.")
        raise PlayerError("The author profile did not open.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("inspect", "prepare", "copy-link"))
    p.add_argument("--author", required=True)
    p.add_argument("--caption", required=True)
    p.add_argument("--duration", type=int)
    a = p.parse_args()
    player = Player(a.author, a.caption, a.duration)
    if a.action == "inspect":
        out = player.inspect()
    elif a.action == "prepare":
        out = player.prepare()
    else:
        out = {"share_url": player.copy_share_url()}
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (PlayerError, OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit(str(exc))
