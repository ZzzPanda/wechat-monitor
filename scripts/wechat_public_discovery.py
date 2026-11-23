"""Discover public-account articles through the signed-in WeChat desktop interface."""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import logging
import re
import time

import numpy as np

from wechat_account_search import AccountSearch, active_appended_tab, clipboard, open_from_main, windows
from wechat_paths import check_drive
from wechat_player import PlayerError, author_matches, center, guest, lines, norm, runs


# Only public article URLs or the public account identifier leave the desktop.
# Profile and article addresses may have additional temporary query parameters.
READ_ADDRESS = """
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import re, subprocess, sys
value = subprocess.run(['xsel', '--clipboard', '--output'], capture_output=True).stdout.decode(errors='replace')
try:
    u = urlsplit(value.strip())
    if u.netloc != 'mp.weixin.qq.com' or u.scheme not in ('http','https'):
        sys.exit()
    q = parse_qs(u.query, keep_blank_values=True)
    unique = all(len(values) == 1 for values in q.values())
    if sys.argv[1] == 'profile':
        biz = q.get('__biz', [''])[0]
        if unique and u.path.startswith('/mp/profile') and re.fullmatch(r'[A-Za-z0-9_+/=-]{5,192}', biz):
            print(biz)
    elif re.fullmatch(r'/s/[A-Za-z0-9_-]+', u.path):
        print('https://mp.weixin.qq.com' + u.path)
    elif u.path == '/s' and unique and all(q.get(k, [''])[0] for k in ('__biz','mid','idx','sn')):
        print(urlunsplit(('https',u.netloc,u.path,urlencode([(k,q[k][0]) for k in ('__biz','mid','idx','sn')]),'')))
except ValueError:
    pass
"""


def search_hit(image, query):
    """The exact-name result must be labeled 公众号 below the account name."""
    rows = lines(image.crop((140, 240, min(810, image.width), min(690, image.height))))
    positions = []
    for tag in rows:
        if not (norm(tag['text']) == '公众号' and tag['x'] < 45):
            continue
        for row in rows:
            name = re.sub(r'\s+(媒体|政务|教育|企业|组织|医疗)\s*$', '', row['text']).strip()
            if (row['x'] < 45 and 18 <= tag['y']-row['y'] <= 55
                    and author_matches(name, query)):
                x, y = center(row)
                positions.append((140+x, 240+y))
    if len(positions) > 1:
        raise PlayerError('微信返回多个同名公众号，无法唯一核实。')
    return positions[0] if positions else None


def account_header(image, name):
    left = image.width//2 - 160
    rows = lines(image.crop((left, 63, left+382, 161)))
    # Vision can append the separate live-status badge to the rendered account name.
    # Keep the exact match first, including for account names containing “直播中”.
    title = next((r for r in rows if r['x'] < 32 and r['y'] < 58
                  and (author_matches(r['text'], name) or
                       norm(re.sub(r'\s+◎\s*直播中\s*$', '', r['text'])) == norm(name))), None)
    if title:
        subject = next((r['text'] for r in rows if r['x'] < 32 and
                        19 <= r['y']-title['y'] <= 47), '')
        return dict(name=name, description=subject)


def profile_access_notice(image):
    labels = [norm(r['text']) for r in lines(image.crop((0, 60, image.width, image.height)))]
    if any(label in ('去验证', '完成验证', '安全验证', '微信安全验证', '环境异常', '当前环境异常') or
           '完成验证后即可继续访问' in label for label in labels):
        return '微信要求完成安全验证，请在微信桌面查看。'
    if any(label in ('请登录', '请先登录', '重新登录', '请登录微信', '请先登录微信', '微信登录',
                     '扫码登录', '登录已过期') or '请使用微信扫码登录' in label for label in labels):
        return '微信提示需要登录，请在微信桌面查看。'
    if any('访问过于频繁' in label or label in ('该公众号已注销', '该账号已被封禁')
           for label in labels):
        return '微信限制了当前主页的访问，请在微信桌面查看。'
    return None


@dataclass(frozen=True)
class ArticleRow:
    x: int
    y: int
    signature: str


def article_rows(im):
    """Every entry, including secondary entries, has its own title and reading count."""
    left = im.width//2 - 245
    top = 145  # Under the sticky account and category bars.
    result = []
    # Against cover photos, Vision can miss the same reading count on a narrow
    # or a wide crop. Merge both views, matching the line's fixed left margin.
    for start, stop in ((left+5, left+197), (left, left+490)):
        for row in lines(im.crop((start, top, stop, im.height))):
            if (abs(start + row['x'] - left - 20) > 12
                    or not re.match(r'^阅[读讀]\s*[:：]?\s*[\d十百千万+＋]', row['text'])):
                continue
            baseline = top+row['y']
            y = baseline - 16
            if y < top+3 or any(abs(other.y-y) < 18 for other in result):
                continue
            # Exclude changing counters and page chrome from the checkpoint.
            title = im.crop((left+16, max(top, baseline-28), left+340, baseline-1)).convert('L')
            digest = hashlib.sha256(title.tobytes()).hexdigest()
            result.append(ArticleRow(left+100, y, digest))
    # Pale counters on cover photos sometimes disappear in OCR. The desktop
    # renders regular secondary thumbnails as 80px squares with white gutters.
    # Headline photos touch both card edges and have a ~208px aspect-ratio box.
    # An overlapping title/counter already accounts for that image.
    rgb = np.asarray(im.convert('RGB'))
    card_band = (rgb[:, left-10] == 237).all(axis=1)
    card_band[:top] = False
    thumbnail = (rgb[:, left+395:left+460].min(axis=2) < 245).mean(axis=1) > .15
    gutters = ((rgb[:, left+475:left+483].min(axis=(1, 2)) >= 253)
               & (rgb[:, left+380:left+385].min(axis=(1, 2)) >= 253))
    cover = np.logical_and.reduce([(rgb[:, left+a:left+b].min(axis=2) < 248).mean(axis=1) > .3
                                   for a, b in ((3, 13), (475, 484))])
    squares = [(a, b) for a,b in runs(thumbnail & gutters & card_band) if 76 <= b-a <= 80]
    covers = [(a, b) for a,b in runs(cover & card_band) if 145 <= b-a <= 214]
    for spans, full_width in ((squares, False), (covers, True)):
        for a, b in spans:
            # Only a recognized title inside the same photo accounts for a
            # wide cover. The immediately following title may be the next item.
            if b >= im.height-3 or any(a-8 <= r.y <= b for r in result):
                continue
            y = int((a+b)//2)
            if y < top+8:
                continue
            region = im.crop((left+20, y-12, left+460, y+12)).convert('L')
            digest = hashlib.sha256(region.tobytes()).hexdigest()
            result.append(ArticleRow(left+180 if full_width else left+435, y, digest))
    return sorted(result, key=lambda r: r.y)


def list_markers(im):
    left = im.width//2 - 245
    rows = lines(im.crop((left, 145, left+490, im.height)))
    end = any(norm(row['text']) in ('已无更多消息','暂无消息','没有更多消息','没有更多了',
                                     '暂无文章','没有更多内容','已没有更多了','已无更多内容')
              and abs(center(row)[0]-245) < 55 for row in rows)
    date_label = re.compile(r'(?:今天|昨天|前天|(?:星期|周)[一二三四五六日天]|'
                            r'(?:\d{4}年)?\d{1,2}月\d{1,2}日|(?:\d{4}[-./])?\d{1,2}[-./]\d{1,2})')
    dated = any(row['x'] <= 30 and date_label.fullmatch(row['text'].strip(' .。'))
                for row in rows)
    return end, dated


def card_gaps(im):
    """The visible feed has 20px of background between rounded white cards."""
    left = im.width//2 - 245
    pixels = np.asarray(im.convert('RGB'))
    # Both side margins and the entire span of the card must show the actual
    # feed background. White spacing between secondary titles is inside a card.
    strip = pixels[:, left-4:left+494:2]
    matches = (strip == 237).all(axis=(1, 2))
    matches[:145] = False
    return [int(start) for start, stop in runs(matches) if stop-start >= 12]


def collapsed_pins(im):
    left = im.width//2 - 245
    rows = lines(im.crop((left-8, 145, left+499, im.height)))
    headings = [r for r in rows if norm(r['text']) == '置顶' and r['x'] <= 32]
    for row in rows:
        count = re.fullmatch(r'(\d{1,2})个内容', norm(row['text']))
        if (count and row['x'] < 43 and any(18 < row['y']-h['y'] < 90 for h in headings)):
            return int(count[1]), left+455, 145+center(row)[1]
    return None


class PublicProfile(AccountSearch):
    def __init__(self, name):
        super().__init__(name, '读取公众号公开文章列表')
        self.stream = None

    @contextmanager
    def opened_tab(self, opener):
        before = self.tab_bar()
        old_count = len(before[0])
        try:
            opener()
            def new_tab(im):
                current, active, _ = self.tab_bar(im)
                return len(current) == old_count+1 and active == old_count
            self.wait(new_tab, '微信未打开独立标签页，请检查微信桌面。', 8)
            self._point(self.size[0]-116, 72)
            yield
        finally:
            current = self.tab_bar()
            if len(current[0]) == old_count+1 and current[1] == old_count:
                self.press('ctrl+w')
                time.sleep(.2)
                current = self.tab_bar()
            if len(current[0]) == old_count:
                self.select_tab(before[1], current)

    def public_address(self, kind):
        _, _, button = self.tab_bar()
        self._point(button, 25, click=True)
        clipboard('wechat-public-address')
        try:
            self.press('ctrl+a','ctrl+c')
            for _ in range(5):
                answer = guest('python3','-c',READ_ADDRESS,kind).decode().strip()
                if answer:
                    return answer
                time.sleep(.1)
            return ''
        finally:
            clipboard('')
            self.press('Escape')
            self._point(self.size[0]-116, 72)

    @contextmanager
    def account_result(self, query, hit):
        tabs, search_tab, _ = self.tab_bar()
        timeout = f'微信未显示「{query}」的公众号主页标题。'

        def checked_header(image):
            info = account_header(image, query)
            if info and not active_appended_tab(self.tab_bar(image), len(tabs)):
                raise PlayerError('核验公众号时活动标签页已改变。')
            return info

        def find_again(image):
            current, active, _ = self.tab_bar(image)
            if len(current) != len(tabs) or active != search_tab:
                raise PlayerError('未能返回原搜索标签页，无法重新打开公众号。')
            return search_hit(image, query)

        for attempt in range(2):
            if attempt:
                hit = self.wait(find_again, '原搜索页未再次显示该公众号的确切匹配结果。', 5)
            with self.opened_tab(lambda: self._point(*hit, click=True)):
                try:
                    info = self.wait(checked_header, timeout, 10)
                except PlayerError as exc:
                    if str(exc) != timeout:
                        raise
                    image = self.image()
                    if not active_appended_tab(self.tab_bar(image), len(tabs)):
                        raise PlayerError('核验公众号时活动标签页已改变。') from exc
                    notice = profile_access_notice(image)
                    if notice:
                        raise PlayerError(notice) from exc
                    if attempt:
                        raise PlayerError(f'两次打开「{query}」的搜索结果后均未识别到主页标题。') from exc
                    logging.warning('%s 将从原搜索页重新核对并打开结果。', timeout)
                    continue
                yield info
                return

    @contextmanager
    def profile(self, query):
        ids = windows('^WeChatAppEx$') or open_from_main()
        if len(ids) != 1:
            raise PlayerError('请在 Docker 微信桌面保留一个微信浏览器窗口。')
        guest('xdotool','windowactivate','--sync',ids[0])
        self._check()
        # Treat even a URL-like query as text within the search form.
        direct = re.fullmatch(r'[\w -]+', query) and re.search('[㐀-鿿]', query)
        with self.opened_tab(lambda: self.address(query if direct else '微信公众号')):
            if not direct:
                self.wait(lambda im: any('搜索' in r['text'] for r in lines(im.crop((40,72,875,135)))),
                          '微信搜一搜未打开。')
                self._point(245,105,click=True)
                clipboard(query)
                self.press('ctrl+a','ctrl+v','Return')
            def match(im):
                hit = search_hit(im, query)
                if hit:
                    return hit
                rows = lines(im.crop((45,145,850,min(550, im.height))))
                if any('暂无搜索结果' in r['text'] or '没有找到' in r['text'] for r in rows):
                    return 'empty'
            timeout = '微信搜索未显示结果，请检查桌面登录与网络。'
            try:
                hit = self.wait(match, timeout, 9)
            except PlayerError as exc:
                # An ordinary results page without an exact match is a valid
                # empty search. A crash, verification screen or expired login
                # is an error and should not be cached as zero results.
                im = self.image()
                terms = [norm(r['text']) for r in lines(im.crop((44, 76, 760, 135)))]
                categories = lines(im.crop((45, 145, 850, 183)))
                if (str(exc) != timeout or norm(query) not in terms
                        or not any('账号' in r['text'] for r in categories)):
                    raise
                hit = 'empty'
            if hit == 'empty':
                yield None
                return
            with self.account_result(query, hit) as info:
                biz = self.public_address('profile')
                if not biz:
                    # Some client versions use an internal profile address. Resolve
                    # the public identity from a real article opened on this profile.
                    from wechat_public_article import read_article
                    rows = self.wait(article_rows, '该公众号主页未提供可核实身份的文章。', 10)
                    errors = []
                    for row in rows[:3]:
                        try:
                            article = read_article(self.article_url(row))
                        except (PlayerError, ValueError, RuntimeError) as exc:
                            errors.append(str(exc)[:120])
                            continue
                        if norm(article['author']) == norm(query):
                            biz = article['biz']
                            break
                    if not biz and errors:
                        raise PlayerError('无法从该公众号的公开文章核实身份：' + errors[-1])
                if not biz:
                    raise PlayerError('未能核实公众号的公开标识。')
                yield dict(info, id=biz, biz=biz)

    def select_articles(self):
        left = self.size[0]//2-245
        def category(im):
            # The tab positions vary with the available categories. Read individual
            # text runs from the header line so Vision cannot merge adjacent tabs.
            for r in lines(im.crop((left-7, 175, left+460, min(473,im.height)))):
                if '全部' not in r['text']:
                    continue
                y = 175 + center(r)[1]
                band = im.crop((left-7,y-16,left+460,y+16))
                ink = np.asarray(band.convert('L')) < 168
                spans = []
                for a,b in runs(ink.any(axis=0)):
                    if spans and a-spans[-1][1] <= 7:
                        spans[-1][1] = int(b)
                    else:
                        spans.append([int(a),int(b)])
                for a,b in spans:
                    if 24 <= b-a <= 65:
                        tile = band.crop((max(a-3,0),0,b+3,32))
                        words = lines(tile.resize((tile.width*2,64)))
                        if any(norm(row['text'])=='文章' for row in words):
                            return left-7+(a+b)//2, y
            return None
        pos = self.wait(category, '主页未显示「文章」分类，无法确认文章列表。', 7)
        self._point(*pos, click=True)
        time.sleep(.7)

    def expand_pinned(self):
        # WeChat may move *individual articles* out of their original push into
        # one collapsed pinned card. The remaining ordinary card may start at
        # idx=2 or idx=3. Expand this card even during incremental sweeps.
        im = self.wait(lambda image: image if article_rows(image) or collapsed_pins(image)
                       or list_markers(image)[0] else None, '微信尚未显示公众号文章列表。', 12)
        pin = collapsed_pins(im)
        if not pin:
            left = im.width//2 - 245
            rows = lines(im.crop((left-8, 145, left+130, im.height)))
            # A client can remember an already expanded pinned section. Its
            # exact size is unknown, so wait for a dated view in bounded mode.
            return -1 if any(norm(r['text']) == '置顶' and r['x'] <= 32 for r in rows) else 0
        count, x, y = pin
        self._point(x,y,click=True)
        self.wait(lambda image: collapsed_pins(image) is None and article_rows(image),
                  '未能展开公众号置顶文章，已停止以免遗漏。', 8)
        return count

    def article_url(self, row):
        with self.opened_tab(lambda: self._point(row.x,row.y,click=True)):
            until = time.monotonic()+7
            while time.monotonic() < until:
                url = self.public_address('article')
                if url:
                    return url
                time.sleep(.35)
            raise PlayerError('文章标签页未返回可归档的公开微信链接。')

    def scroll(self):
        self._point(self.size[0]//2,self.size[1]*3//4)
        # Each wheel step is about 150 pixels in the tested client. Always overlap.
        guest('sh','-c','[ "$(xdotool getactivewindow)" = "$1" ] && '
              'xdotool click --repeat 3 --delay 110 5','_',self.window)
        time.sleep(.65)


def search_public_accounts(query):
    query = query.strip() if isinstance(query,str) else ''
    if not norm(query) or len(query)>64 or not query.isprintable():
        raise ValueError('请输入 1 到 64 个字符的公众号名称。')
    check_drive()
    browser = PublicProfile(query)
    with browser.profile(query) as account:
        return [account] if account else []


def history_pages(profile):
    previous = None
    still = 0
    page = 0
    prefix = ''
    past_pinned = False
    while True:
        im = profile.image()
        left = im.width//2-245
        snapshot = np.asarray(im.crop((left,145,left+490,im.height)).resize((68,180)).convert('L'), dtype='int16')
        still = still+1 if previous is not None and (abs(snapshot-previous)>12).mean()<.01 else 0
        rows = article_rows(im)
        end, dated = list_markers(im)
        if end:
            # The client transiently shows its empty-state label before loading.
            time.sleep(1 if rows or page else 3)
            newer = profile.image()
            if not list_markers(newer)[0] or article_rows(newer) != rows:
                continue
        digest = hashlib.sha256(repr([(r.y//4,r.signature) for r in rows]).encode()).hexdigest()
        prefix = hashlib.sha256((prefix+digest).encode()).hexdigest()
        yield page, rows, prefix, end, still >= 3, past_pinned, card_gaps(im)
        if end or still >= 3:
            return
        previous = snapshot
        past_pinned = past_pinned or dated
        page += 1
        profile.scroll()


def crawl_public_account(entry, *, mode, on_article, should_stop, max_pages=None,
                         known=None, checkpoint=None, on_boundary=None):
    if mode not in ('history','incremental','baseline'):
        raise ValueError('无效的公众号采集模式。')
    if max_pages is not None and (type(max_pages) is not int or max_pages<1):
        raise ValueError('采集页数必须为正整数。')
    check_drive()
    if should_stop():
        return dict(complete=False, reason='已停止', pages=0)
    profile = PublicProfile(entry['name'])
    with profile.profile(entry['name']) as verified:
        if not verified or verified['biz'] != (entry.get('biz') or entry['id']):
            raise PlayerError('微信没有返回同一公众号，已停止采集。')
        profile.select_articles()
        pin_count = profile.expand_pinned()
        pages = iter(history_pages(profile))
        limit_start = None if pin_count else 0
        # Reopen the profile from the top on each job. A prefix shortcut is only
        # valid if every intervening viewport is identical to the saved traversal.
        if mode=='history' and isinstance(checkpoint,dict) and checkpoint.get('v')==1:
            target = checkpoint.get('next_page')
            if type(target) is int and target>0 and isinstance(checkpoint.get('prefix'),str):
                restored = False
                for number, rows, digest, end, stalled, _, _ in pages:
                    if should_stop():
                        return dict(complete=False, reason='已停止', pages=0)
                    if number+1 >= target or end or stalled:
                        restored = number+1==target and digest==checkpoint['prefix'] and bool(rows)
                        if restored and type(checkpoint.get('limit_start')) is int:
                            limit_start = checkpoint['limit_start']
                        break
                if not restored:
                    profile.press('Home')
                    time.sleep(.7)
                    pages = iter(history_pages(profile))
        seen = {}
        old_pages = failures = 0
        tail_url = None
        for number, rows, prefix, end, stalled, past_pinned, gaps in pages:
            page_old = bool(rows)
            fresh_urls = 0
            seeking = tail_url
            closing = None
            last_url = last_y = None
            for row in rows:
                if closing is not None and row.y >= closing:
                    break
                if should_stop():
                    return dict(complete=False, reason='已停止', pages=number+1)
                try:
                    url = profile.article_url(row)
                except PlayerError as exc:
                    failures += 1
                    page_old = False
                    logging.warning('未能打开%s的文章：%s',entry['name'],exc)
                    continue
                if seeking:
                    if url != seeking:
                        continue
                    # The preceding viewport ended inside this push. Re-find
                    # its last opened article and archive only remaining rows
                    # up to this card's lower border, not the next push.
                    seeking = None
                    closing = next((y for y in gaps if y > row.y), None)
                last_url, last_y = url, row.y
                if url not in seen:
                    fresh_urls += 1
                    if known is not None and known(url):
                        seen[url] = True
                    else:
                        seen[url] = bool(on_article(url))
                page_old = page_old and seen[url]
            # The newest content follows pinned cards. An unmoving window or
            # overlap cannot provide an independent incremental boundary.
            old_pages = old_pages+1 if past_pinned and page_old and fresh_urls else 0
            if limit_start is None and (past_pinned or 0 < pin_count < len(seen)):
                # The optional range starts in the dated timeline. Merely
                # expanding a pinned card must not consume the entire budget.
                limit_start = number
            reason = ''
            bounded = False
            if seeking:
                return dict(complete=False, reason='翻页后无法重新定位限屏末尾的推送', pages=number+1)
            if end:
                reason = '微信已显示文章列表到底'
            elif stalled:
                return dict(complete=False, reason='微信列表未继续滚动，未确认到底',pages=number+1)
            elif ((mode == 'incremental' and (old_pages >= 2 or tail_url)) or
                  (max_pages and limit_start is not None and number-limit_start+1 >= max_pages)):
                if last_url is None:
                    return dict(complete=False, reason='未能核实末尾推送的边界', pages=number+1)
                if any(y > last_y for y in gaps) or closing is not None:
                    if mode == 'incremental':
                        reason = '连续两屏文章均早于增量起点，已检查至推送卡片末尾'
                    else:
                        reason = f'已检查指定的 {max_pages} 屏文章及末张推送卡片的余下篇目'
                        bounded = True
                else:
                    tail_url = last_url
            if reason:
                if failures:
                    reason += f'，{failures} 条未能打开'
                return dict(complete=failures==0, reason=reason,pages=number+1,bounded=bounded)
            if not tail_url and mode=='history' and on_boundary and on_boundary(dict(
                    v=1,next_page=number+1,prefix=prefix if rows and failures==0 else None,
                    limit_start=limit_start)):
                return dict(complete=False,yielded=True,reason='稍后继续扫描历史文章',pages=number+1)
        return dict(complete=False,reason='历史列表中止，未确认到底',pages=0)
