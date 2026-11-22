from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
import oci_cache as cache


def checksum(content):
    return 'sha256:' + hashlib.sha256(content).hexdigest()


class Reply(io.BytesIO):
    def __init__(self, data, status, content_range=''):
        super().__init__(data)
        self.status = status
        self.headers = {'Content-Range': content_range}


class TestRegistry:
    def __init__(self, parts, support_range=True):
        self.parts = parts
        self.requests = []
        self.support_range = support_range

    def get(self, endpoint, headers):
        content = self.parts[endpoint.split('/')[-1]]
        self.requests.append((endpoint, headers.get('Range')))
        if self.support_range and 'Range' in headers:
            first, last = map(int, headers['Range'].removeprefix('bytes=').split('-'))
            return Reply(content[first:last+1], 206, f'bytes {first}-{last}/{len(content)}')
        return Reply(content, 200)


class OciCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.content = b'abcdefghi'
        self.desc = {'digest': checksum(self.content), 'size': len(self.content)}

    def test_resumed_ranges_sealed_after_verification(self):
        blobs = self.root/'blobs'
        blobs.mkdir()
        scratch = self.root/'scratch'
        parts = scratch/self.desc['digest'][7:]
        parts.mkdir(parents=True)
        (parts/'0').write_bytes(self.content[:3])
        registry = TestRegistry({self.desc['digest']: self.content})
        with patch.object(cache, 'CHUNK', 3), ThreadPoolExecutor(max_workers=2) as pool:
            cache.download_blob(registry, blobs, scratch, self.desc, pool)
            cache.download_blob(registry, blobs, scratch, self.desc, pool)
        self.assertCountEqual([r for _,r in registry.requests], ['bytes=3-5','bytes=6-8'])
        self.assertEqual((blobs/self.desc['digest'][7:]).read_bytes(), self.content)
        self.assertFalse(parts.exists())

    def test_corrupt_part_rejected_and_whole_body_fallback(self):
        blobs = self.root/'blobs'
        blobs.mkdir()
        scratch = self.root/'scratch'
        parts = scratch/self.desc['digest'][7:]
        parts.mkdir(parents=True)
        (parts/'0').write_bytes(b'bad')
        with patch.object(cache, 'CHUNK', 3), ThreadPoolExecutor(max_workers=2) as pool:
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                cache.download_blob(TestRegistry({self.desc['digest']: self.content}), blobs, scratch, self.desc, pool)
            self.assertFalse(parts.exists())
            self.assertFalse(list(blobs.iterdir()))
            fallback = TestRegistry({self.desc['digest']:self.content}, support_range=False)
            cache.download_blob(fallback, blobs, scratch, self.desc, pool)
            self.assertEqual([r for _,r in fallback.requests], ['bytes=0-2',None])

    def test_layer_retries_without_redownloading_completed_offsets(self):
        config = json.dumps({'os': 'linux', 'architecture': 'arm64'}).encode()
        manifest = json.dumps({'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                               'config': {'size': len(config), 'digest': checksum(config)},
                               'layers': [self.desc]}).encode()
        registry = TestRegistry({checksum(b): b for b in (config, manifest, self.content)})
        fail_offset = 6
        calls = []
        fetch = cache.fetch_part
        def simulate_outage(registry, key, total, offset, length, dest, ranged):
            if key == self.desc['digest']:
                calls.append(offset)
                if offset == fail_offset and calls.count(fail_offset) == 1:
                    raise cache.URLError('connection lost after other ranges completed')
            return fetch(registry, key, total, offset, length, dest, ranged)
        with patch.object(cache, 'Registry', return_value=registry), patch.object(cache, 'CHUNK', 3), \
             patch.object(cache, 'WORKERS', 1), patch.object(cache.time, 'sleep'), \
             patch.object(cache, 'fetch_part', side_effect=simulate_outage):
            cache.prepare('docker.io/library/python@' + checksum(manifest), self.root/'layout')
        self.assertEqual(calls, [0, 3, 6, 6])

    def test_arm64_index_and_offline_rerun(self):
        config = json.dumps({'os':'linux','architecture':'arm64'}).encode()
        manifest = json.dumps({'mediaType':'application/vnd.oci.image.manifest.v1+json','schemaVersion':2,
                               'config': {'size':len(config),'digest':checksum(config)},'layers':[self.desc]}).encode()
        platform_manifest = {'size':len(manifest),'digest':checksum(manifest),'platform':{'os':'linux','architecture':'arm64'}}
        index = json.dumps({'manifests':[{'platform':{'os':'linux','architecture':'amd64'}},platform_manifest]}).encode()
        registry = TestRegistry({checksum(b):b for b in [index,config,manifest,self.content]})
        ref = 'docker.io/library/python:example@'+checksum(index)
        target = self.root/'layout'
        with patch.object(cache, 'Registry', return_value=registry):
            self.assertEqual(cache.prepare(ref,target),target)
            registry.requests.clear()
            cache.prepare(ref,target)
        self.assertEqual(registry.requests,[])
        desc = json.loads((target/'index.json').read_text())['manifests'][0]
        self.assertEqual(desc['digest'],checksum(manifest))
        self.assertEqual(desc['annotations']['org.opencontainers.image.ref.name'],'base')
