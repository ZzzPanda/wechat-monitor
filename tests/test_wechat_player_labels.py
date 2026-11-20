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

    def test_normal_words_and_title_ending(self):
        self.assertEqual(caption_text('当我用Al拍照⋯ 展开'), caption_text('当我用AI拍照'))
        self.assertNotEqual(caption_text('Alpha'), caption_text('AIpha'))
        self.assertNotEqual(caption_text('逐渐展开'), caption_text('逐渐'))

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


if __name__ == '__main__':
    unittest.main()
