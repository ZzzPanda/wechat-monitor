"""Enumerate works through the visible, signed-in WeChat Channels profile."""
import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import numpy as np
from PIL import Image

from wechat_account_search import AccountSearch, PROFILE, open_from_main, profile_name, search_accounts, windows
from wechat_paths import check_drive
from wechat_player import PlayerError, author_matches, guest, lines, runs

BASELINE_WORKS = 10
PINNED_SLOTS = 3
KNOWN_BOUNDARY = 3


@dataclass(frozen=True)
class Tile:
    x: int
    y: int
    signature: bytes


def tiles(image):
    """Read card positions at the bottom edge of the thumbnail, above its caption."""
    ink = np.asarray(image.convert('L')) < 240
    h, w = ink.shape
    # WeChat draws a dark duration strip and rounded corners followed by white
    # space. Detect this even when a thumbnail is mostly white or has no caption.
    counts = ink.sum(axis=1).astype(int)
    edges = np.flatnonzero(counts[:-1] - counts[1:] > 68) + 1
    found = []
    for bottom in edges:
        if not 175 <= bottom < h-5:
            continue
        strip = ink[bottom-12:bottom-4].mean(axis=0) >= .85
        for left, right in runs(strip):
            left, right = int(left), int(right)
            width = right-left
            if left < 24 or right > w-12 or not 95 <= width <= 350:
                continue
            center = slice(left+8, right-8)
            if (ink[bottom-4:bottom, center].mean() < .72
                    or ink[bottom:bottom+4, center].mean() > .02
                    or ink[bottom-38:bottom-24, center].mean() < .55):
                continue
            x = (left + right)//2
            # The crop avoids rounded corners, likes and duration (all can change).
            crop = image.crop((x-36, bottom-61, x+36, bottom-29)).convert('L')
            signature = hashlib.sha256(bytes(np.asarray(crop.resize((18, 8), Image.Resampling.BILINEAR)) // 16)).digest()
            found.append(Tile(x, int(bottom-33), signature))
    return found


def overlapping_tiles(cards, previous, before, now):
    """Skip a repeated cover only if the surrounding grid scrolled by the same distance."""
    if before is None or before.shape != now.shape:
        return set()
    shifts = {}
    repeated = set()
    for card in cards:
        for old in previous:
            if abs(card.x-old.x) > 3 or card.signature != old.signature:
                continue
            delta = old.y-card.y
            if not 0 <= delta < now.shape[0]-90:
                continue
            if delta not in shifts:
                height = now.shape[0]-delta
                change = abs(before[delta::5] - now[:height:5])
                shifts[delta] = (change > 16).mean() < .06
            if shifts[delta]:
                repeated.add(card)
                break
    return repeated


def resolved_profile(entry):
    name = entry['name']
    url = entry.get('url')
    if not url:
        candidates = [row for row in search_accounts(name) if row['name'] == name]
        if len(candidates) != 1:
            raise PlayerError('旧名单的名称没有唯一匹配，请先在管理页确认并关联视频号主页。')
        url = candidates[0]['url']
    parts = urlsplit(url)
    ids = parse_qs(parts.query).get('username', [])
    if (parts.scheme == 'https' and parts.netloc == 'channels.weixin.qq.com'
            and parts.path == '/web/pages/profile' and len(ids) == 1
            and re.fullmatch(r'[A-Za-z0-9_.-]{1,220}@finder', ids[0])):
        identity = ids[0]
        if entry.get('id') and entry['id'] != identity:
            raise PlayerError('名单中的账号标识与微信主页不一致，请重新关联。')
        return PROFILE + '?' + urlencode({'username': identity}), identity
    if (parts.scheme == 'https' and parts.netloc == 'weixin.qq.com' and
            re.fullmatch(r'/f/[A-Za-z0-9_-]+', parts.path) and not parts.query and not parts.fragment):
        return url, None
    raise PlayerError('请在管理页关联已核验的微信视频号主页。')


def footer(image, name):
    w, h = image.size
    top = int(h*.68)
    rows = [dict(r, y=r['y']+top) for r in lines(image.crop((0, top, w, h)))]
    authors = [r for r in rows if .038*w < r['x'] < .23*w and r['y'] > .78*h and author_matches(r['text'], name)]
    if len(authors) != 1:
        return None
    author = authors[0]
    captions = [r for r in rows if r['x'] <= .10*w and author['y']-100 <= r['y'] <= author['y']-20]
    caption = min(captions, key=lambda r: abs(r['y'] - (author['y']-50))) if captions else None
    return image, author, caption


class ProfileDiscovery(AccountSearch):
    def __init__(self, name):
        super().__init__(name, '读取主页上的公开作品')
        self.stream = None

    def open_profile(self, url, identity):
        ids = windows('^WeChatAppEx$') or open_from_main()
        if len(ids) != 1:
            raise PlayerError('请在微信桌面保留一个视频号窗口。')
        guest('xdotool', 'windowactivate', '--sync', ids[0])
        self.address(url)
        self.wait(self._header_name, '微信未打开指定作者的主页。', 12)
        actual = self.address()
        if not actual or (identity and actual != identity):
            raise PlayerError('打开的微信主页与所选视频号标识不一致。')
        self.profile_id = actual
        self.profile_tab = self.tab_bar()[1]
        self.press('Home')
        time.sleep(.35)

    def _header_name(self, im):
        return profile_name(lines(im.crop((0, 0, im.width, 205))), self.author)

    def open_work(self, card):
        self._point(card.x, card.y, click=True)
        _, author, caption = self.wait(lambda im: footer(im, self.author), '未能核实卡片对应的作品作者。', 7)
        # The exact title may be split across several drawn labels. The source is
        # already the verified profile and the newly opened player's author must match.
        self.duration = self.seek_origin = None
        self.title = caption or {'x': self.size[0]//35, 'y': author['y']-50}
        self._pause()
        image, author, _ = self.wait(lambda im: footer(im, self.author), '分享前无法再次核验作者。', 3)
        return self.copy_visible_share_url(image, author)

    def return_to_grid(self, previous_count):
        tabs, active, _ = self.tab_bar()
        if active != self.profile_tab:
            if len(tabs) == previous_count+1 and active == len(tabs)-1:
                self.press('ctrl+w')
            self.select_tab(self.profile_tab)
        if self.address() != self.profile_id:
            raise PlayerError('作品关闭后未能返回该作者主页，已停止以避免收录其他账号。')

    def scroll(self):
        w, h = self.size
        self._point(w//2, h*3//4)
        # Two wheel notches are less than one visible screen so adjacent views overlap.
        guest('sh', '-c', '[ "$(xdotool getactivewindow)" = "$1" ] && '
              'xdotool click --repeat 2 --delay 70 5', '_', self.window)
        time.sleep(.5)


def crawl_profile(entry, *, mode, known, on_work, should_stop):
    """Caller owns the saver lock. on_work(url) runs while its verified player is open."""
    if mode not in ('baseline', 'history', 'incremental'):
        raise ValueError('未知的作品采集模式。')
    check_drive()
    if should_stop():
        return dict(complete=False, reason='已停止')
    profile = ProfileDiscovery(entry['name'])
    url, identity = resolved_profile(entry)
    profile.open_profile(url, identity)
    processed = failures = known_run = still = 0
    previous_image = None
    previous_cards = []
    previous_layout = None
    seen = set()

    def result(detail, success=True):
        if failures:
            detail += f'；{failures} 张作品卡未能核验分享链接'
        return dict(complete=bool(success and not failures), reason=detail)

    while not should_stop():
        im = profile.image()
        cards = tiles(im)
        region = im.crop((30, 147, im.width-30, im.height-12))
        snapshot = np.asarray(region.convert('L').resize((80, region.height)), dtype='int16')
        repeated = overlapping_tiles(cards, previous_cards, previous_image, snapshot)
        markers = lines(im.crop((20, 125, im.width-20, im.height)))
        captions = [r['text'] for r in markers if any(abs(r['x']+20-card.x) < 140
                    and 33 <= r.get('y', -1000)+125-card.y <= 86 for card in cards)]
        layout = ([(c.x//6, c.y//6) for c in cards], captions)
        if previous_image is not None:
            identical = (abs(snapshot-previous_image) > 12).mean() < .012
            still = still + 1 if identical or (cards and previous_layout == layout) else 0
        previous_image, previous_layout = snapshot, layout
        at_end = any(abs(r['x'] + r['width']/2 + 20 - im.width/2) < im.width*.04
                     and re.fullmatch(r'[\s·—\-]*(?:没有更多了|暂无视频|暂无作品|还没有作品|没有更多作品)[\s·—\-]*',
                                      r['text'].strip()) for r in markers)
        for card in cards:
            if should_stop():
                return result('已停止', False)
            if card in repeated:
                continue
            count = len(profile.tab_bar()[0])
            try:
                try:
                    work_url = profile.open_work(card)
                except (PlayerError, ValueError):
                    failures += 1
                    continue
                if work_url in seen:
                    continue
                seen.add(work_url)
                was_known = mode == 'incremental' and known(work_url)
                if should_stop():
                    return result('已停止', False)
                if not was_known:
                    on_work(work_url)
                processed += 1
                known_run = known_run + 1 if was_known and processed > PINNED_SLOTS else 0
            finally:
                profile.return_to_grid(count)
            # Do not use the first three (possibly pinned) cards as a history boundary.
            if mode == 'incremental' and known_run >= KNOWN_BOUNDARY:
                return result('置顶位置之后已连续遇到 3 个历史作品')
            if mode == 'baseline' and processed >= BASELINE_WORKS:
                return result(f'已建立最近 {processed} 条作品的增量基线')
        if at_end:
            return result(f'微信显示列表到底，核实 {processed} 条')
        if still >= 3:
            return result(f'微信列表滚动后未继续加载，已核实 {processed} 条；未确认列表底部', False)
        previous_cards = cards
        profile.scroll()
    return result('已停止', False)
