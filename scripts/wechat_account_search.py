#!/usr/bin/env python3
"""Find Channels profiles through the signed-in Linux WeChat search interface."""

import argparse
from contextlib import ExitStack
import fcntl
import io
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlencode

import numpy as np
from PIL import Image

from wechat_frames import FrameStream
from wechat_paths import DATA_ROOT, check_drive
from wechat_player import Player, PlayerError, center, guest, lines, norm, runs


PROFILE = 'https://channels.weixin.qq.com/web/pages/profile'
MAX_RESULTS = 4


def clipboard(text):
    guest('sh', '-c', 'printf %s "$1" | xsel --clipboard --input', '_', text)


def tabs(image):
    # The Linux client draws the tabs and its square address/search button on one border.
    pixels = np.asarray(image.crop((0, 0, image.width, 12)).convert('L'))
    edges = [(int(a), int(b)) for a, b in runs(pixels[6] < 233) if a > 100 and b-a >= 24]
    if len(edges) < 2 or not 24 <= edges[-1][1] - edges[-1][0] <= 42:
        raise PlayerError('无法识别微信浏览器标签栏，请收起地址栏并显示视频号窗口。')
    mids = [(a + b) // 2 for a, b in edges]
    active = [i for i, x in enumerate(mids[:-1]) if pixels[10, x] > 245]
    if len(active) != 1:
        raise PlayerError('无法确定微信当前打开的标签页。')
    return mids[:-1], active[0], mids[-1]


def nav_item(image, top, bottom, label):
    # OCR often merges all the tabs into one line. Read their separately rendered labels.
    band = image.crop((40, top, min(880, image.width), bottom))
    ink = np.asarray(band.convert('L')) < 155
    groups = []
    for start, end in runs(ink.any(axis=0)):
        if groups and start - groups[-1][1] < 10:
            groups[-1][1] = int(end)
        else:
            groups.append([int(start), int(end)])
    groups = [(a, b) for a, b in groups if 14 <= b - a <= 125]
    if not groups:
        return None
    sheet = Image.new('RGB', (280, 80 * len(groups)), 'white')
    for index, (a, b) in enumerate(groups):
        crop = band.crop((max(0, a-2), 0, min(band.width, b+2), band.height))
        sheet.paste(crop.resize((crop.width*2, crop.height*2)), (5, index*80+4))
    for item in lines(sheet):
        if norm(item['text']) == label:
            a, b = groups[min(len(groups)-1, center(item)[1] // 80)]
            return 40 + (a+b)//2, (top+bottom)//2
    return None


def cards(rows, query):
    # In the video-account filter, each visible avatar has a right-aligned text column.
    column = [r for r in rows if 150 <= r['x'] <= 169 and r['y'] > 245]
    groups = []
    for row in sorted(column, key=lambda r: r['y']):
        if not groups or row['y'] - groups[-1][-1]['y'] > 52:
            groups.append([])
        groups[-1].append(row)
    return [group[0] for group in groups if norm(query) in norm(group[0]['text'])][:MAX_RESULTS]


def profile_name(rows, name):
    return next((r for r in rows if 140 < r['x'] < 255 and 60 < r['y'] < 128
                 and norm(r['text']) == norm(name)), None)


# Read only the public Finder ID from the address selected by the user interface.
# Temporary query parameters stay inside the WeChat container and are never returned.
READ_FINDER_ID = """
import re, subprocess
from urllib.parse import parse_qs, urlsplit
text = subprocess.run(['xsel','--clipboard','--output'], capture_output=True).stdout
try:
    u = urlsplit(text.decode().strip())
    ids = parse_qs(u.query).get('username', [])
    if (u.scheme == 'https' and u.netloc == 'channels.weixin.qq.com'
        and u.path == '/web/pages/profile' and len(ids) == 1
        and re.fullmatch(r'[A-Za-z0-9_.-]{1,220}@finder', ids[0])):
        print(ids[0])
except (ValueError, UnicodeError):
    pass
"""


def windows(class_name, visible=True):
    options = ['--onlyvisible'] if visible else []
    try:
        return guest('xdotool', 'search', *options, '--class', class_name).decode().split()
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return []
        raise


def open_from_main():
    # Only capture the narrow icon rail, which excludes the conversations and messages.
    instruction = '请登录微信，在主窗口左侧点击「视频号」蝴蝶图标，再重新搜索。'
    main = [w for w in windows('^wechat$', visible=False)
            if guest('xdotool', 'getwindowname', w).decode().strip() == '微信']
    if len(main) != 1:
        raise PlayerError(instruction)
    window = main[0]
    guest('xdotool', 'windowactivate', '--sync', window)
    geometry = guest('xdotool', 'getwindowgeometry', '--shell', window).decode()
    height = re.search(r'^HEIGHT=(\d+)$', geometry, re.M)
    if not height or int(height[1]) < 440:
        raise PlayerError(instruction)
    height = min(520, int(height[1])-100)
    data = guest('ffmpeg', '-v', 'error', '-nostdin', '-f', 'x11grab', '-window_id', window,
                 '-grab_x', '10', '-grab_y', '100', '-video_size', f'54x{height}',
                 '-draw_mouse', '0', '-framerate', '20', '-probesize', '32', '-analyzeduration', '0',
                 '-i', ':1', '-frames:v', '1', '-c:v', 'png', '-f', 'image2pipe', 'pipe:1')
    rail = np.asarray(Image.open(io.BytesIO(data)).convert('L')) < 145
    icon = np.asarray(Image.open(Path(__file__).with_name('wechat_channels_button.png'))) < 145
    patches = np.lib.stride_tricks.sliding_window_view(rail, icon.shape)
    similarity = (patches & icon).sum(axis=(-2, -1)) / np.maximum(1, (patches | icon).sum(axis=(-2, -1)))
    matches = np.argwhere(similarity > .88)
    if len(matches) != 1:
        raise PlayerError(instruction)
    y, x = matches[0]
    guest('sh', '-c', '[ "$(xdotool getactivewindow)" = "$1" ] && '
          'xdotool mousemove --window "$1" "$2" "$3" click 1', '_', window,
          int(x) + 10 + icon.shape[1]//2, int(y) + 100 + icon.shape[0]//2)
    for _ in range(20):
        if ids := windows('^WeChatAppEx$'):
            return ids
        time.sleep(.3)
    raise PlayerError(instruction)


class AccountSearch(Player):
    def image(self):
        if self.stream is not None:
            # A buffered picture may predate the last click. Wait for a fresh
            # acquisition before inspecting this new state of the window.
            after = time.monotonic()
            try:
                while True:
                    stamp, image = self.stream.read(timeout=.6)
                    if stamp >= after:
                        return image
            except (OSError, RuntimeError):
                self.stream = None
        return self._image()[1]

    def tab_bar(self, image=None):
        # A tab close briefly paints the old and new active tabs simultaneously.
        for attempt in range(5):
            try:
                return tabs(image if image is not None else self.image())
            except PlayerError:
                image = None
                if attempt == 4:
                    raise
                time.sleep(.15)

    def press(self, *keys):
        self._check()
        guest('sh', '-c', '[ "$(xdotool getactivewindow)" = "$1" ] || exit 1; '
              'shift; xdotool key --clearmodifiers "$@"', '_', self.window, *keys)

    def address(self, value=None, bar=None):
        _, _, button = bar or self.tab_bar()
        self._point(button, 25, click=True)
        if value is not None:
            clipboard(value)
            self.press('ctrl+a', 'ctrl+v', 'Return')
            return
        clipboard('wechat-profile-selection')
        try:
            self.press('ctrl+c')
            for _ in range(5):
                identity = guest('python3', '-c', READ_FINDER_ID).decode().strip()
                if identity:
                    return identity
                time.sleep(.08)
        finally:
            # Clearing the selection also removes the transient address from the clipboard.
            clipboard('')
            self.press('Escape')
        return None

    def wait(self, read, message, seconds=7):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            im = self.image()
            result = read(im)
            if result:
                return result
            time.sleep(.16)
        raise PlayerError(message)

    def select_tab(self, index, bar=None):
        mids, active, _ = bar or self.tab_bar()
        if index < len(mids) and index != active:
            self._point(mids[index], 25, click=True)

    def results(self, query):
        # Punctuation resembling a URL belongs in the search form, not in the address bar.
        natural_name = re.fullmatch(r'[\w -]+', query) and any('㐀' <= ch <= '鿿' for ch in query)
        term = query if natural_name else '微信视频号'
        self.address(term)
        def search_page(im):
            rows = lines(im.crop((40, 72, min(875, im.width), 135)))
            return any('搜索' in r['text'] and r['x'] > 600 for r in rows)
        self.wait(search_page, '微信搜一搜未打开，请检查登录和网络。')
        if term != query:
            self._point(250, 105, click=True)
            clipboard(query)
            self.press('ctrl+a', 'ctrl+v', 'Return')
        item = self.wait(lambda im: nav_item(im, 151, 181, '账号'), '找不到搜一搜的「账号」分类。')
        self._point(*item, click=True)
        item = self.wait(lambda im: nav_item(im, 211, 240, '视频号'), '找不到账号分类中的「视频号」筛选。')
        self._point(*item, click=True)
        # WeChat highlights the filter before replacing the mixed-account results.
        # Wait for the displayed names/positions to settle across independent frames.
        time.sleep(.75)
        previous = None
        def filtered(im):
            nonlocal previous
            x, y = item
            ink = np.asarray(im.convert('L'))[y-11:y+10, x-25:x+25]
            if (ink < 65).sum() < 28:
                return None
            left, top = 40, 246
            rows = [dict(r, x=r['x'] + left, y=r['y'] + top)
                    for r in lines(im.crop((left, top, min(im.width, 865), im.height)))]
            if any(146 <= r['x'] < 172 for r in rows) or any(
                marker in r['text'] for r in rows for marker in ('未找到', '无结果', '暂无', '没有', '未搜到')):
                signature = [(r['y'], r['text']) for r in rows if 146 <= r['x'] < 172]
                if signature == previous:
                    return cards(rows, query), self.tab_bar(im)
                previous = signature
                time.sleep(.3)
        return self.wait(filtered, '微信未显示可确认的视频号搜索结果。')

    def find(self, query):
        ids = windows('^WeChatAppEx$') or open_from_main()
        if len(ids) != 1:
            raise PlayerError('请在已登录的微信中打开一个视频号或搜一搜窗口。')
        guest('xdotool', 'windowactivate', '--sync', ids[0])
        self._check()
        self.stream = None
        with ExitStack() as stack:
            try:
                self.stream = stack.enter_context(FrameStream(self.window, self.size, (0, 0, *self.size), fps=12))
            except (OSError, RuntimeError):
                pass  # A one-shot window grab remains available on older containers.
            try:
                return self.find_in_window(query)
            finally:
                self.stream = None

    def find_in_window(self, query):
        original = self.image()
        original_bar = self.tab_bar(original)
        old_tabs, old_active, _ = original_bar
        original_id = None
        header = lines(original.crop((0, 0, original.width, 250)))
        if any(140 < r['x'] < 255 and 60 < r['y'] < 128 for r in header):
            original_id = self.address(bar=original_bar)
        search_index, owns_search = None, False
        output = []
        try:
            matches, search_bar = self.results(query)
            current, search_index, _ = search_bar
            owns_search = len(current) == len(old_tabs) + 1 and search_index == len(old_tabs)
            for hit in matches:
                name = hit['text'].strip()
                tab_count = len(current)
                self._point(*center(hit), click=True)
                identity = None
                try:
                    try:
                        profile_image, rows = self.wait(
                            lambda im: rows_if_profile(im, name), '微信未打开选中作者的主页。', 5)
                    except PlayerError:
                        continue
                    profile_bar = self.tab_bar(profile_image)
                    identity = self.address(bar=profile_bar)
                    if identity and all(row['id'] != identity for row in output):
                        description = [r['text'] for r in rows if (145 < r['x'] < 220 and 120 < r['y'] < 153)
                                       or (40 < r['x'] < 90 and 185 < r['y'] < 247
                                           and not any(s in r['text'] for s in ('公众号', '原创内容')))]
                        output.append(dict(name=profile_name(rows, name)['text'].strip(), id=identity,
                                           url=PROFILE+'?'+urlencode({'username': identity}),
                                           description=' · '.join(description)))
                finally:
                    bar = self.tab_bar()
                    opened, active, _ = bar
                    if identity and len(opened) == tab_count + 1 and active == tab_count:
                        self.press('ctrl+w')
                        bar = self.tab_bar()
                    current = bar[0]
                    self.select_tab(search_index, bar)
            if matches and not output:
                raise PlayerError('微信返回了候选账号，但未能核实其主页链接，请重试。')
            return output
        finally:
            bar = self.tab_bar()
            current, active, _ = bar
            # A fresh address-bar search opens a new last tab. Only close our own tab.
            if search_index is None and len(current) == len(old_tabs)+1 and active == len(old_tabs):
                search_index, owns_search = active, True
            if search_index is not None and owns_search:
                self.select_tab(search_index, bar)
                self.press('ctrl+w')
            self.select_tab(old_active)
            if original_id and self.address() != original_id:
                self.address(PROFILE+'?'+urlencode({'username': original_id}))


def rows_if_profile(image, name):
    # The verified name, badge and description all live in the top profile header.
    rows = lines(image.crop((0, 0, image.width, 250)))
    if profile_name(rows, name):
        return image, rows
    return None


def search_accounts(q):
    q = q.strip() if isinstance(q, str) else ''
    if not norm(q) or len(q) > 64 or any(not char.isprintable() for char in q):
        raise PlayerError('请输入 1 到 64 个字符的视频号名称关键词。')
    check_drive()
    # The host worker serializes this operation with playback via save.lock.
    return AccountSearch('微信视频号', '搜索可见的作者名片').find(q)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query')
    query = parser.parse_args().query
    try:
        check_drive()
        with (DATA_ROOT/'state/wechat-pilot/save.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(search_accounts(query), ensure_ascii=False))
    except BlockingIOError:
        parser.exit(1, '微信正在执行搜索或保存任务。\n')
    except PlayerError as exc:
        parser.exit(1, f'{exc}\n')
