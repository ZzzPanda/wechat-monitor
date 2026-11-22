"""Drive the worker and durable queue with local fixtures and no model calls."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root/'dashboard' if (root/'dashboard/server.py').is_file() else root/'state/dashboard-app'),
                str(root/'scripts')]
import video_analysis_worker as worker
from video_analysis_queue import Queue
from video_analysis_routes import worker_call


class VideoAnalysisWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.artifacts = self.root/'analysis'
        self.artifacts.mkdir()
        self.queue = Queue(self.root/'analysis.sqlite3', lease_seconds=10, retry_seconds=5)
        self.now = 100
        self.api = Mock()
        self.api.call.side_effect = lambda op, **fields: worker_call(self.queue, dict(op=op, **fields))
        self.config = ('fixture-token', 'fixture-cli', self.root/'fixture-model.bin')
        self.windows = [dict(start_ms=0, end_ms=45000), dict(start_ms=45000, end_ms=80000)]
        self.evidence = dict(duration_ms=80000, windows=self.windows)

        for replacement in (
            patch.object(worker, 'DATA_ROOT', self.root),
            patch.object(worker, 'ARTIFACTS', self.artifacts),
            patch('video_analysis_queue.time.time', side_effect=lambda: self.now),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def enqueue(self, *, custom=False):
        folder = '自定义媒体' if custom else '微信视频号'
        video = self.root/'media'/folder/'样本.mp4'
        video.parent.mkdir(parents=True)
        video.write_bytes(b'fixture-video')
        size, digest = worker.file_signature(video)
        relative = f'./{folder}/{video.name}' if custom else video.name
        return video, self.queue.enqueue(dict(work_key='a'*64, sha256=digest, path=relative, size=size))

    def prepare(self, args):
        args.output.mkdir()
        worker.write_json(args.output/'evidence.json', self.evidence)

    def run_claim(self, claim):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            worker.run_job(self.api, claim, self.config)

    def job(self, source):
        return self.queue.get(source['work_key'], source['sha256'])

    def test_retry_reuses_preparation_and_completed_window(self):
        video, job = self.enqueue(custom=True)
        first_result = dict(analysis=dict(summary='第一段', observations=[
            dict(evidence_id='f1_1', kind='frame', timestamp_ms=3000, detail='第一段画面'),
        ]))
        second_result = dict(analysis=dict(summary='第二段', observations=[
            dict(evidence_id='f2_1', kind='frame', timestamp_ms=47000, detail='第二段画面'),
        ]))
        combined = dict(model=worker.MODEL, analysis=dict(
            summary='全片摘要', topics=['主题'], evidence_ids=['f2_1', 'f1_1', 'f2_1'],
        ))
        with (patch.object(worker, 'prepare', side_effect=self.prepare) as prepare,
              patch.object(worker, 'analyze_window', side_effect=[
                  first_result, worker.ModelError('暂时限流'), second_result,
              ]) as analysis,
              patch.object(worker, 'request_model', return_value=combined) as synthesis):
            self.run_claim(self.queue.claim())
            waiting = self.job(job)
            self.assertEqual((waiting['status'], waiting['completed'], waiting['total']), ('retry_wait', 1, 2))
            self.assertIsNone(self.queue.claim())
            self.now = 105
            self.run_claim(self.queue.claim())

        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[0].video, video)
        self.assertEqual([event.args[2]['start_ms'] for event in analysis.call_args_list], [0, 45000, 45000])
        synthesis.assert_called_once()
        completed = self.job(job)
        self.assertEqual((completed['status'], completed['completed']), ('succeeded', 2))
        self.assertEqual(completed['result']['summary'], '全片摘要')
        self.assertEqual([item['timestamp_ms'] for item in completed['result']['observations']], [3000, 47000])

    def test_reclaimed_lease_cannot_call_model_after_preparation(self):
        _, job = self.enqueue()
        first_claim = self.queue.claim()
        replacement = None

        def expire_lease(args):
            nonlocal replacement
            self.prepare(args)
            self.now = 110
            replacement = self.queue.claim()

        with (patch.object(worker, 'prepare', side_effect=expire_lease),
              patch.object(worker, 'analyze_window') as analysis,
              patch.object(worker, 'request_model') as synthesis):
            self.run_claim(first_claim)

        self.assertEqual(replacement['id'], job['id'])
        self.assertNotEqual(replacement['lease_token'], first_claim['lease_token'])
        self.assertEqual(self.job(job)['status'], 'running')
        analysis.assert_not_called()
        synthesis.assert_not_called()
        self.assertEqual([call.args[0] for call in self.api.call.call_args_list], ['update', 'update'])

    def test_different_digest_cannot_use_cached_evidence(self):
        video, job = self.enqueue()
        evidence = self.artifacts/job['id']/'prepared'/'evidence.json'
        evidence.parent.mkdir(parents=True)
        evidence.write_text(json.dumps(self.evidence))
        video.write_bytes(b'fixture-other')
        self.assertEqual(video.stat().st_size, job['size'])

        with (patch.object(worker, 'prepare') as prepare,
              patch.object(worker, 'analyze_window') as analysis,
              patch.object(worker, 'request_model') as synthesis):
            self.run_claim(self.queue.claim())

        self.assertEqual(self.job(job)['status'], 'failed')
        self.assertIn('成片文件已变化', self.job(job)['error'])
        prepare.assert_not_called()
        analysis.assert_not_called()
        synthesis.assert_not_called()

    def test_different_digest_after_extraction_cannot_commit_evidence(self):
        video, job = self.enqueue()

        def change_source(args):
            self.prepare(args)
            video.write_bytes(b'fixture-other')

        with (patch.object(worker, 'prepare', side_effect=change_source),
              patch.object(worker, 'analyze_window') as analysis,
              patch.object(worker, 'request_model') as synthesis):
            self.run_claim(self.queue.claim())

        self.assertEqual(self.job(job)['status'], 'failed')
        self.assertFalse((self.artifacts/job['id']/'prepared').exists())
        analysis.assert_not_called()
        synthesis.assert_not_called()


if __name__ == '__main__':
    unittest.main()
