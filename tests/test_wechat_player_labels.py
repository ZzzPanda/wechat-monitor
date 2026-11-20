"""Desktop OCR can merge visible labels and confuse the standalone token AI."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import numpy as np
from PIL import Image, ImageDraw

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from wechat_player import (Player, PlayerError, author_matches, caption_text,
                           opening_handle, opening_stamp, visible_clock_position)


def row(text, x, y):
    return dict(text=text, x=x, y=y, width=90, height=22)


class PlayerLabels(unittest.TestCase):
    def test_identity_with_combined_ui_labels(self):
        p = Player('数字生命卡兹克', '当我用AI逆向爆改公司硬件 #ai #数字生命卡兹克')
        p.size = (1158, 960)
        texts = [row('当我用Al逆向爆改公司硬件⋯ 展开', 34, 838), row('数字生命卡兹克 畅 已关注', 98, 889)]
        self.assertEqual(p._identity(texts), tuple(texts))
        self.assertFalse(author_matches('数字生命卡兹克分号 已关注', p.author))
        self.assertFalse(author_matches('数字生命卡兹克X 已关注', p.author))

    def test_split_caption_and_comparison_emoji_on_real_player(self):
        player = Player('卡尔的AI沃茨', '2025🆚2026！10类AI工具汇总，20秒看懂！')
        player.size = (1158, 960)
        author = row('卡尔的AI沃茨 必', 100, 888)
        rest = dict(row('10类AI工具汇总，20秒看懂！⋯展开', 146, 836), width=230)
        for prefix in ('2025u2026！', '2025uS 2026！', '2025🆚2026！'):
            first = dict(row(prefix, 32, 838), width=109)
            label, account = player._identity([rest, first, author])
            self.assertEqual((label['x'], label['y'], label['width'], label['height']), (32, 836, 344, 24))
            self.assertEqual(account, author)
            self.assertIsNone(player._identity([first, dict(rest, text='另一个不同的作品'), author]))
            self.assertIsNone(player._identity([first, rest, dict(author, text='其他作者')]))

    def test_single_caption_box_beside_translation_button(self):
        player = Player('测试作者', '十分钟了解视频创作流程，从剧本到成片')
        player.size = (1158, 960)
        title = dict(row('十分钟了解视频创作流程', 32, 838), width=210)
        author = row('测试作者', 100, 888)
        self.assertEqual(player._identity([title, row('翻译', 250, 838), author]), (title, author))

    def test_expand_button_follows_exact_public_headline(self):
        player = Player('卡尔的AI沃茨', '让机器人当我的跟拍摄影师，首次开箱和公园跟拍！\n#机器人开箱')
        player.size = (1158, 960)
        author = row(player.author, 100, 888)
        for ending in ('！•展升、', '！一展开', '！…展汗', '！一展天', '！总 一展开'):
            title = row('让机器人当我的跟拍摄影师，首次开箱和公园跟拍' + ending, 36, 836)
            self.assertEqual(player._identity([title, author]), (title, author))
            self.assertIsNone(player._identity([dict(title, text='不同的作品标题' + ending), author]))

    def test_verify_open_player_from_footer_when_full_frame_ocr_misses(self):
        p = Player('测试作者', '这是一条被切开的标题')
        p.size = (1158, 960)
        frame = Image.new('RGB', p.size)
        p._image = Mock(return_value=(0, frame))
        p._open_card = Mock()
        top = int(frame.height * .68)
        footer = [row(p.caption, 32, 838-top), row(p.author, 100, 888-top)]
        with patch('wechat_player.lines', side_effect=[[], footer]):
            self.assertIs(p._inspect(allow_open=True), frame)
        self.assertEqual((p.title['y'], p.name['y']), (838, 888))
        p._open_card.assert_not_called()

    def test_scrolled_profile_and_ambiguous_prefix(self):
        p = Player('数字生命卡兹克', '当我用AI逆向爆改公司硬件 #ai')
        p._point = Mock()
        rows = [row('公众号：数字生命卡兹克', 48, 46), row('206条原创内容', 46, 84),
                row('直播回放', 108, 210), row('当我用Al逆向爆改公司', 668, 522)]
        p._open_card(rows)
        p._point.assert_called_once()
        with self.assertRaises(PlayerError):
            p._open_card(rows + [row('当我用AI逆向爆改公司', 860, 522)])
        p._point.assert_called_once()

    def test_loading_clock_then_ready_or_wrong_duration(self):
        p = Player('数字生命卡兹克', '当我用AI复刻出Mac的付费软件', 169)
        p.size = (1158, 960)
        p.title = row('作品标题', 34, 837)
        p._point = Mock()
        image = Image.new('RGB', (1158, 150))
        draw = ImageDraw.Draw(image)
        draw.rectangle((192, 97, 846, 102), fill='#999999')
        for x in (32, 44):
            draw.rectangle((x, 95, x + 4, 104), fill='white')
        p._image = Mock(return_value=(0, image))
        with patch('wechat_player.lines', side_effect=[[row('0:00 / 0:00', 73, 89)],
                                                       [row('0:01 / 2:49', 73, 89)]]):
            self.assertEqual(p._toolbar(), dict(duration=169, position=1, paused=False))
        self.assertEqual(p._image.call_count, 2)
        with patch('wechat_player.lines', return_value=[row('0:01 / 2:50', 73, 89)]):
            with self.assertRaisesRegex(PlayerError, 'differs from expected'):
                p._toolbar()

    def test_pillarboxed_portrait_uses_each_observed_edge(self):
        p = Player('数字生命卡兹克', '实测10个好用的prompt合集')
        p.title = row('实测10个好用的prompt合集', 34, 838)
        im = Image.new('RGB', (1158, 960), 'black')
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 1157, 52), fill='#dddddd')
        draw.line((0, 0, 0, 959), fill='white')  # one-pixel window border
        draw.rectangle((325, 53, 834, 959), fill='#90abcc')
        draw.line((196, 790, 887, 790), fill='white', width=4)  # native control layer
        self.assertEqual(p._get_crop(im), (325, 54, 510, 906))
        draw.rectangle((0, 900, 1157, 959), fill='black')
        with self.assertRaises(PlayerError):
            p._get_crop(im)

    def test_verified_square_work_uses_the_player_viewport(self):
        p = Player('测试作者', '这是方形视频的标题')
        p.window, p.size = 123, (1159, 961)
        p.title, p.name = row(p.caption, 34, 838), row(p.author, 98, 889)
        im = Image.new('RGB', p.size, 'black')
        draw = ImageDraw.Draw(im)
        draw.rectangle((0, 0, 1158, 52), fill='#eeeeee')
        draw.rectangle((230, 120, 929, 819), fill='#95aabb')
        p._inspect = Mock(return_value=im)
        with patch('wechat_player.time.sleep'):
            self.assertEqual(p._confirm_crop(im), (0, 54, 1158, 906))
        # Even a tall window has no measured gutter in the fallback mode.
        p.crop = (0, 54, 900, 1100)
        self.assertFalse(p._has_pillarbox())
        # A changed programme must still stop the operation during crop retries.
        p._inspect.side_effect = PlayerError('The visible author and caption do not both match this work.')
        with patch('wechat_player.time.sleep'), self.assertRaisesRegex(PlayerError, 'author and caption'):
            p._confirm_crop(im)

    def test_a_verified_landscape_keeps_its_tight_crop(self):
        p = Player('测试作者', '这是横屏视频的标题')
        p.title = row(p.caption, 34, 838)
        im = Image.new('RGB', (1152, 960), 'black')
        ImageDraw.Draw(im).rectangle((0, 105, 1151, 752), fill='#95aabb')
        self.assertEqual(p._confirm_crop(im), (0, 105, 1152, 648))

    def test_portrait_track_crosses_the_picture_while_playing_and_paused(self):
        for paused, track_height in [(False, 5), (True, 5), (True, 0), (False, 26)]:
            with self.subTest(paused=paused, track_height=track_height):
                p = Player('测试作者', '这是竖屏视频', 14)
                p.size, p.crop = (1158, 960), (324, 54, 510, 906)
                p.title = row('测试作品', 34, 836)
                p._point = Mock()
                image = Image.new('L', p.size)
                draw = ImageDraw.Draw(image)
                draw.rectangle((324, 54, 833, 959), fill=200)
                draw.rectangle((42, 782, 47, 798), fill=230)
                if not paused:
                    draw.rectangle((53, 782, 58, 798), fill=230)
                if track_height:
                    draw.rectangle((197, 788, 889, 787 + track_height), fill=104)
                p._image = lambda box: (0., image.crop((box[0], box[1],
                                                         box[0]+box[2], box[1]+box[3])))
                clock = row('0:04 / 0:14', 78, 783 - (p.title['y'] - 245))
                with patch('wechat_player.lines', return_value=[clock]):
                    if track_height == 5:
                        self.assertEqual(p._toolbar()['paused'], paused)
                        self.assertEqual(p.controls['seek'], (198, 790))
                        self.assertEqual(p.controls['right'], 890)
                    else:
                        with self.assertRaisesRegex(PlayerError, 'track is not recognizable'):
                            p._toolbar()

    def test_clock_merged_with_native_play_icon(self):
        p = Player('测试作者', '这是合并识别测试', 137)
        p.size = (1158, 960)
        p.title = row('作品标题', 32, 838)
        p._point = Mock()
        image = Image.new('L', (1158, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((197, 194, 889, 198), fill=104)
        draw.polygon([(45, 186), (45, 205), (60, 196)], fill=230)
        p._image = lambda box: (0., image)
        merged = row('- 0:27/2:17', 44, 186)
        digits = row('0:27/2:17', 10, 186)
        with patch('wechat_player.lines', side_effect=[[merged], [digits]]):
            self.assertEqual(p._toolbar(), dict(duration=137, position=27, paused=True))
        self.assertEqual(p.controls['clock_left'], 78)
        self.assertTrue(45 <= p.controls['button'][0] <= 59)

    def test_clock_visible_only_in_full_window_uses_controls_from_that_frame(self):
        player = Player('卡尔的AI沃茨', '让机器人当我的跟拍摄影师')
        player.size = (1158, 960)
        player.title = row('让机器人当我的跟拍摄影师', 36, 831)
        player._point = Mock()
        full = Image.new('L', player.size)
        draw = ImageDraw.Draw(full)
        draw.rectangle((197, 790, 889, 794), fill=104)
        for x in (42, 54):
            draw.rectangle((x, 783, x + 5, 802), fill=230)
        trailer = Image.new('L', (full.width, 255))
        player._image = lambda box=None: (0., trailer if box else full)
        clock = dict(row('0:04/ 2:14', 76, 784), height=16)
        # Match the observed full-window geometry. An unrelated timer inside
        # the film is outside the toolbar band.
        labels = [clock, dict(clock, y=300), player.title, row('卡尔的AI沃茨', 100, 888)]
        def recognize(image):
            return labels if image.size == player.size else []
        with patch('wechat_player.lines', side_effect=recognize):
            self.assertEqual(player._toolbar(), dict(duration=134, position=4, paused=False))
            self.assertEqual(player.controls['button'], (51, 792))
            self.assertEqual(player.controls['seek'], (198, 792))
            labels.append(dict(clock, y=690))
            with patch('wechat_player.time.monotonic', side_effect=(0., 5.)), \
                    self.assertRaisesRegex(PlayerError, 'unambiguous player clock'):
                player._toolbar()

    def test_normal_words_and_title_ending(self):
        self.assertEqual(caption_text('当我用Al拍照⋯ 展开'), caption_text('当我用AI拍照'))
        self.assertNotEqual(caption_text('Alpha'), caption_text('AIpha'))
        self.assertNotEqual(caption_text('逐渐展开'), caption_text('逐渐'))
        for ending in ('！•展升', '！展开', '！”展开', '！…、展开'):
            self.assertEqual(caption_text('首次开箱和公园跟拍' + ending), caption_text('首次开箱和公园跟拍'))

    def test_clock_requires_one_reading_with_exact_duration(self):
        image = Image.new('RGB', (120, 42))
        for printed, expected in [(['25：19/25：20'], 1519), (['25:19/25:21'], None),
                                  (['25:21/25:20'], None), (['25:19/25:20', '25:20/25:20'], None)]:
            with self.subTest(printed=printed), patch('wechat_player.lines',
                    return_value=[{'text': text} for text in printed]):
                self.assertEqual(visible_clock_position(image, 1520), expected)

    def test_short_start_is_observed_by_motion_on_the_true_track(self):
        # Native 14s portrait geometry and sampled handle positions, with an imposed
        # 750ms delay between input and the initial frame reaching the viewport.
        samples = []
        for t, x in [(0, 2), (.5, 23), (1, 49), (1.5, 75), (2, 101)]:
            roi = Image.new('L', (136, 140))
            draw = ImageDraw.Draw(roi)
            draw.rectangle((12, 122, 135, 126), fill=104)
            draw.ellipse((12+x-7, 124-8, 12+x+7, 124+8), fill=255)
            detected = opening_handle(roi)
            self.assertAlmostEqual(detected, x, delta=.5)
            samples.append((.75 + t, detected))
        aligned = opening_stamp(samples, 0., 14/692)
        self.assertAlmostEqual(aligned, .75 - .125, delta=.06)
        self.assertIsNone(opening_stamp(samples[1:], 0., 14/692))
        self.assertIsNone(opening_stamp([(t * 3, x) for t, x in samples], 0., 14/692))
        self.assertIsNone(opening_stamp([(t, 0.) for t, _ in samples], 0., 14/692))

    def test_prepared_opening_plays_without_reseeking_and_allows_icon_to_settle(self):
        player = Player('测试作者', '让AI说人话的四类提示语', 175)
        player.controls = dict(button=(49, 790), seek=(193, 790), right=890)
        player._point, player._inspect = Mock(), Mock()
        opening = dict(paused=True, position=0)
        player._toolbar = Mock(side_effect=[opening, opening, dict(paused=False, position=4)])
        with patch('wechat_player.time.sleep'), patch('wechat_player.time.monotonic', side_effect=(100., 104.)):
            self.assertEqual(player.restart(), 100.)
        player._point.assert_called_once_with(49, 790, click=True)
        player._inspect.assert_called_once_with()

    def test_start_resets_past_the_opening_only_for_the_verified_work(self):
        player = Player('测试作者', '让AI说人话的四类提示语', 175)
        player.controls = dict(button=(49, 790), seek=(193, 790), right=890)
        opening = dict(paused=True, position=0)
        advanced = dict(paused=False, position=28)
        player._point, player._inspect = Mock(), Mock()
        player._toolbar = Mock(side_effect=[opening, advanced, opening, dict(paused=False, position=1)])
        player.restart()
        self.assertEqual([args for args, _ in player._point.call_args_list], [(49, 790), (193, 790), (49, 790)])
        player._point.reset_mock()
        player._inspect.side_effect = [None, PlayerError('目标作品已变化')]
        player._toolbar = Mock(side_effect=[opening, advanced])
        with self.assertRaisesRegex(PlayerError, '目标作品已变化'):
            player.restart()
        player._point.assert_called_once_with(49, 790, click=True)

    def test_long_work_requires_near_end_printed_clock(self):
        p = Player('测试作者', '这是测试视频', 1520)
        p.start, p.window, p.size = 100., 1, (1120, 930)
        p.controls = dict(seek=(201, 710), right=850, clock_left=75)
        p.title, p._point = {'x': 34}, Mock()
        p.identity_box = (34, 760, 250, 95)
        p.identity_regions = [(slice(0, 14), slice(0, 50)), (slice(70, 85), slice(3, 53))]
        p.reference = [np.ones((14, 50), bool), np.ones((15, 50), bool)]
        now = [0.]

        class EndFrames:
            def __init__(self, window, size, box, fps):
                left, top, w, h = box
                frame = np.zeros((h, w), dtype='uint8')
                frame[760-top:774-top, 34-left:84-left] = 255
                frame[830-top:845-top, 37-left:87-left] = 255
                # The coarse slider incorrectly rounds this handle all the way to 25:20.
                frame[709-top:713-top, 200-left:850-left] = 250
                frame[704-top:716-top, 844-left:855-left] = 250
                self.work = Image.fromarray(frame)
                self.next = Image.new('L', (w, h))

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, timeout):
                now[0] += .125
                return now[0], self.work if now[0] < p.start + p.duration else self.next

        for reading in (1519, 1517, None):
            now[0] = p.start + p.duration - 4
            with self.subTest(reading=reading), patch('wechat_frames.FrameStream', EndFrames), \
                    patch('wechat_player.time.monotonic', side_effect=lambda: now[0]), \
                    patch('wechat_player.visible_clock_position', return_value=reading):
                if reading == 1519:
                    observed = p.watch()
                    self.assertEqual(observed['last_position'], 1519)
                    self.assertEqual(observed['position_source'], 'visible-clock')
                else:
                    with self.assertRaisesRegex(PlayerError, 'Uncertain end'):
                        p.watch()

    def test_watch_reads_native_labels_over_changing_footage_and_detects_the_next_title(self):
        title = '让机器人当我的跟拍摄影师'
        p = Player('卡尔的AI沃茨', title, 17)
        p.start, p.window, p.size = 100., 1, (1158, 960)
        p.title, p.name = row(title, 34, 838), row(p.author, 98, 889)
        p._point = Mock()
        p.identity_box = (34, 838, 240, 73)
        p.controls = dict(seek=(201, 710), right=850)
        p.identity_regions = [(slice(0, 22), slice(0, 90)), (slice(51, 73), slice(64, 154))]
        # Both the paused reference and unrelated footage can be solid white
        # under the low pixel threshold. Matching pixels alone are ambiguous.
        p.reference = [np.ones((22, 90), bool) for _ in p.identity_regions]
        now = [p.start]

        class OverlayFrames:
            def __init__(self, window, size, box, fps):
                self.box = box

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def read(self, timeout):
                left, top, width, height = self.box
                now[0] += .125
                # A cut can hide the footer briefly. Autoplay keeps the next
                # work's different caption in that same position.
                background = 105
                if now[0] >= 117. or (switch_early and now[0] >= 105.):
                    background = 200
                elif 105. <= now[0] < 106.:
                    background = 150
                frame = np.full((height, width), background, dtype='uint8')
                frame[709-top:713-top, 200-left:850-left] = 250
                position = min(1., (now[0]-p.start)/p.duration)
                handle = round(200 + 649 * position)
                frame[704-top:716-top, handle-left-7:handle-left+7] = 250
                return now[0], Image.fromarray(frame)

        def footer(image):
            background = image.getpixel((0, 0))
            if background == 150:
                return []
            caption = '同一账号的另一个作品' if background == 200 else title
            return [row(caption, 12, 12), row(author, 76, 63)]

        for author, switch_early in ((p.author, False), (p.author, True), ('另一个账号', False)):
            now[0] = p.start
            with self.subTest(author=author, switch_early=switch_early), patch('wechat_frames.FrameStream', OverlayFrames), \
                    patch('wechat_player.time.monotonic', side_effect=lambda: now[0]), \
                    patch('wechat_player.lines', side_effect=footer):
                if switch_early:
                    with self.assertRaisesRegex(PlayerError, 'Uncertain end'):
                        p.watch()
                    self.assertEqual(now[0], 107.5)
                elif author == p.author:
                    seen = p.watch()
                    self.assertEqual(seen['first_change_monotonic'], 117.)
                    self.assertEqual(seen['end_monotonic'], 116.625)
                    self.assertLess(now[0] - 117., .5)
                    self.assertEqual(seen['last_position'], 17)
                else:
                    with self.assertRaisesRegex(PlayerError, 'Wrong programme'):
                        p.watch()

    def test_footer_enlargement_can_resolve_a_misread_final_glyph(self):
        visible_title = '让机器人当我的跟拍摄影师，首次开箱和公园跟拍！'
        p = Player('卡尔的AI沃茨', visible_title + '\n#机器人')
        p.size = (1158, 960)
        p.title = dict(row(visible_title, 36, 836), width=398, height=24)
        p.name = dict(row(p.author, 100, 888), width=124, height=20)
        p.identity_box = (36, 836, 398, 72)
        frame = Image.new('RGB', (876, 304))
        def recognize(image):
            scale = image.width // 422
            label = visible_title if scale == 2 else visible_title[:-1] + '人'
            return [dict(text=text, x=x*scale, y=y*scale, width=w*scale, height=h*scale)
                    for text, x, y, w, h in ((label, 12, 12, 398, 24), (p.author, 76, 64, 124, 20))]
        with patch('wechat_player.lines', side_effect=recognize) as reader:
            self.assertTrue(p._sampled_footer_matches(frame, 24, 616))
            self.assertEqual(reader.call_count, 2)


if __name__ == '__main__':
    unittest.main()
