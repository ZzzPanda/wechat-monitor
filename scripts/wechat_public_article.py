"""Read one public WeChat article using an isolated Chrome profile."""
from datetime import datetime, timedelta, timezone
from collections import namedtuple
from html.parser import HTMLParser
import os
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

BEIJING = timezone(timedelta(hours=8))
BIZ = r'[A-Za-z0-9_+/=-]{1,128}'
NUMBER = r'[1-9][0-9]{0,19}'
FIELDS = {'js_content': 'body', 'activity-name': 'title', 'js_name': 'author',
          'publish_time': 'published'}
VOID = set('area base br col embed hr img input link meta param source track wbr'.split())
BLOCK = set('p div section article li h1 h2 h3 h4 h5 h6 tr blockquote pre'.split())
IDENTITY = re.compile(r'''\bvar\s+(biz|mid|idx|ct)\s*=\s*["']([^"'\s]{1,150})["']''')
BODY_LIMIT = 2_000_000
Frame = namedtuple('Frame', 'tag field hidden control')
HIDDEN_STYLE = re.compile(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b', re.I)
READ_CONTROL = re.compile(r'(?:^|[_-])(?:paywall|(?:pay|paid|payread|login|unlock)[_-]'
                          r'(?:area|mask|bar|btn|button|box|wall|preview|read|reading|dialog|prompt|tips?))'
                          r'(?:$|[_-])', re.I)
READ_RESTRICTION = re.compile(
    r'(?:付费|购买|支付|登录|登陆|订阅)(?:后)?(?:方可|才可|即可|可以|可)?(?:继续)?(?:阅读|查看|解锁)'
    r'(?:本文|本篇|文章)?(?:的)?(?:剩余|余下|全部|全文|完整|正文)'
    r'|(?:付费|购买|支付|登录|登陆|订阅)后(?:方可|才可|即可|可以|可)?继续阅读'
)


class IncompletePage(ValueError):
    """Chrome returned an empty, loading, or partially rendered document."""


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('请输入有效的公众号文章链接。')
    u = urlsplit(value)
    if u.netloc.lower() == 'mp.weixin.qq.com' and u.scheme in ('http', 'https'):
        base = 'https://mp.weixin.qq.com'
        if re.fullmatch(r'/s/[\w-]{1,128}', u.path, re.ASCII):
            return base + u.path
        q = parse_qs(u.query, keep_blank_values=True)
        rules = [('__biz', BIZ), ('mid', NUMBER), ('idx', NUMBER), ('sn', r'[A-Fa-f0-9]{32}')]
        if u.path == '/s' and all(len(q.get(k, [])) == 1 and re.fullmatch(pattern, q[k][0]) for k, pattern in rules):
            return base + '/s?' + urlencode([(k, q[k][0]) for k, _ in rules])
    raise ValueError('链接不属于可核实的公众号文章。')


class ArticlePage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.values = {field: [] for field in FIELDS.values()}
        self.variables = {}
        self.pictures = []
        self.seen_fields = set()
        self.page_notice = ''
        self.read_controls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        parent = self.stack[-1] if self.stack else Frame('', None, False, None)
        field = FIELDS.get(attrs.get('id'), parent.field)
        if attrs.get('id') in FIELDS:
            if field in self.seen_fields:
                raise ValueError('文章页包含重复的正文字段，请复核原文。')
            self.seen_fields.add(field)
        if tag in ('style', 'template', 'noscript', 'script'):
            field = 'script' if tag == 'script' and not parent.field else None
        if field == 'body':
            if tag in BLOCK or tag == 'br':
                self.values[field].append('\n')
            if tag == 'img':
                src = attrs.get('data-src') or attrs.get('src') or ''
                try:
                    image = urlsplit(src)
                except ValueError:
                    image = None
                if image and image.scheme == 'https' and image.netloc.lower() in ('mmbiz.qpic.cn', 'mmbiz.qlogo.cn'):
                    self.pictures.append(src)
        if tag not in VOID:
            classes = (attrs.get('class') or '').split()
            hidden = (parent.hidden or tag in ('head', 'template', 'script', 'style', 'noscript')
                      or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
                      or any(c in ('hidden', 'hide', 'weui-hidden', 'weui-hide', 'dsn') for c in classes)
                      or bool(HIDDEN_STYLE.search(attrs.get('style') or '')))
            control = parent.control
            if not field and not hidden and control is None:
                names = [attrs.get('id') or '', *classes]
                if (tag == 'button' or attrs.get('role') in ('button', 'dialog') or 'weui-btn' in classes or
                        any(READ_CONTROL.search(name) for name in names)):
                    control = len(self.read_controls)
                    self.read_controls.append('')
            self.stack.append(Frame(tag, field, hidden, control))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                if tag in BLOCK and self.stack[i].field == 'body':
                    self.values['body'].append('\n')
                del self.stack[i:]
                return

    def handle_data(self, text):
        if not self.stack:
            return
        frame = self.stack[-1]
        field = frame.field
        if field == 'script':
            for name, value in IDENTITY.findall(text):
                self.variables.setdefault(name, set()).add(value)
        elif field:
            self.values[field].append(text)
        elif not frame.hidden:
            if len(self.page_notice) < 1024:
                self.page_notice += text.strip()[:1024-len(self.page_notice)]
            if frame.control is not None:
                notice = self.read_controls[frame.control]
                if len(notice) < 1024:
                    self.read_controls[frame.control] = notice + text.strip()[:1024-len(notice)]

    def variable(self, name, pattern):
        values = self.variables.get(name, set())
        if len(values) != 1:
            raise ValueError('微信文章缺少唯一的公开标识。')
        value = next(iter(values))
        if not re.fullmatch(pattern, value):
            raise ValueError('微信文章标识格式异常。')
        return value

    def result(self, url):
        if any(READ_RESTRICTION.search(''.join(control.split())) for control in self.read_controls):
            raise ValueError('微信文章提示需要付费或登录才能继续阅读，未取得全文。')
        title = ' '.join(''.join(self.values['title']).split())
        author = ' '.join(''.join(self.values['author']).split())
        body = '\n'.join(line for raw in ''.join(self.values['body']).splitlines()
                         if (line := ' '.join(raw.split())))
        if not body and self.pictures:
            # Some public posts consist entirely of captionless images.
            body = '\n'.join('[图片] ' + src for src in self.pictures)
        if not body or not title or not author:
            if any(t in self.page_notice for t in ('环境异常', '完成验证', '去验证', '已被删除', '内容已删除',
                                                  '内容无法查看', '违规无法查看', '登录后', '付费阅读')):
                raise ValueError('微信文章已受限、失效或需要验证。')
            if (not self.page_notice or self.seen_fields or self.variables or
                    any(t in self.page_notice for t in ('加载中', '正在加载', '正在打开', '无法访问此网站',
                                                        'site can’t be reached', 'ERR_CONNECTION_', 'ERR_TIMED_OUT'))):
                raise IncompletePage('Chrome 返回了空白或尚未加载完整的文章页。')
            raise ValueError('未读到完整的微信文章标题、公众号和正文，页面可能已失效或需要验证。')
        if len(body) > BODY_LIMIT:
            raise ValueError('微信文章正文超出了单篇大小限制。')
        fields = dict(url=public_url(url), title=title, author=author, body=body,
                      biz=self.variable('biz', BIZ), mid=self.variable('mid', NUMBER),
                      idx=int(self.variable('idx', NUMBER)))
        if self.variables.get('ct'):
            seconds = int(self.variable('ct', r'[1-9]\d{8,10}'))
            stamp = datetime.fromtimestamp(seconds, BEIJING)
        else:
            date = re.sub(r'\s+', ' ', ''.join(self.values['published'])).strip()
            match = re.fullmatch(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}):(\d{2})', date)
            if not match:
                raise ValueError('无法核实文章发布时间。')
            stamp = datetime(*map(int, match.groups()), tzinfo=BEIJING)
        fields['published'] = stamp.strftime('%Y-%m-%d %H:%M:%S')
        return fields


def read_article(url):
    url = public_url(url)
    candidates = (shutil.which('google-chrome'), shutil.which('chromium'),
                  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    chrome = next((p for p in candidates if p and os.access(p, os.X_OK)), None)
    if not chrome:
        raise RuntimeError('找不到用于读取微信文章的 Chrome 或 Chromium。')
    from wechat_paths import DATA_ROOT
    with tempfile.TemporaryDirectory(prefix='wechat-article-', dir=DATA_ROOT / 'tmp') as profile:
        command = [chrome, '--headless', '--disable-gpu', '--disable-extensions',
                   '--disable-background-networking', '--no-first-run', '--no-default-browser-check',
                   '--user-data-dir=' + profile, '--virtual-time-budget=15000', '--dump-dom', url]
        for attempt in range(2):
            try:
                result = subprocess.run(command, capture_output=True, check=True, timeout=45)
                page = ArticlePage()
                page.feed(result.stdout.decode('utf-8', errors='replace'))
                page.close()
                return page.result(url)
            except IncompletePage:
                if attempt:
                    raise
            except (subprocess.SubprocessError, OSError) as exc:
                if attempt:
                    raise RuntimeError('Chrome 未能读取文章，请稍后重试。') from exc
            time.sleep(1)
