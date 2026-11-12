import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import os


def load(name):
    spec = importlib.util.spec_from_file_location(name=name.replace('-', '_'), location=Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec=spec)
    spec.loader.exec_module(module=module)
    return module


planner = load(name='ci-plan')
publisher = load(name='play-draft')


class PlanTests(unittest.TestCase):
    def plan(self, **changes):
        values = dict(event='push', ref='refs/heads/release/android-preview', sign=True,
                      upload=False, run_number=5, attempt=1, version='2.11.0')
        values.update(changes)
        return planner.plan(**values)

    def test_preview_signed_without_upload_until_bootstrapped(self):
        self.assertEqual(self.plan(), {'sign': 'true', 'code': '1501', 'upload': 'false'})
        self.assertEqual(self.plan(upload=True)['upload'], 'true')

    def test_pull_requests_and_main_cannot_sign_or_upload(self):
        for changes in [dict(event='pull_request', ref='refs/pull/1/merge'), dict(ref='refs/heads/main'),
                        dict(sign=False, upload=True), dict(ref='refs/heads/release/other', upload=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)

    def test_manual_release_and_version_match(self):
        self.assertEqual(self.plan(event='workflow_dispatch', upload=True)['upload'], 'true')
        with self.assertRaises(ValueError):
            self.plan(ref='refs/tags/v2.12.0')

    def test_run_and_retry_codes_increase_without_collision(self):
        self.assertLess(int(self.plan()['code']), int(self.plan(attempt=2)['code']))
        self.assertLess(int(self.plan(attempt=99)['code']), int(self.plan(run_number=6)['code']))
        for changes in [dict(attempt=100), dict(run_number=0), dict(run_number=21000000)]:
            with self.assertRaises(ValueError):
                self.plan(**changes)


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.payload = b'fake signed candidate for isolated transport test'
        self.metadata = {'package': publisher.PACKAGE, 'signed': True, 'version': '2.11.0',
                         'version_code': 1501, 'bundle_sha256': hashlib.sha256(self.payload).hexdigest()}
        (self.directory / 'hardcharger-play.aab').write_bytes(self.payload)
        (self.directory / 'release-notes.txt').write_text('Internal test release')
        self.write_metadata()
        self.calls = []
        self.previous = [{'name': 'Previous', 'versionCodes': ['1401'], 'status': 'completed'}]
        self.fail_validation = False
        self.wrong_version = False
        self.wrong_hash = False

    def write_metadata(self):
        (self.directory / 'candidate.json').write_text(json.dumps(self.metadata))

    def request(self, *, method, url, body=None):
        self.calls.append((method, url, body))
        if url.endswith('/edits'):
            return {'id': 'edit-1'}
        if method == 'GET':
            return {'releases': self.previous}
        if 'uploadType=media' in url:
            return {'versionCode': 1502 if self.wrong_version else 1501,
                    'sha256': 'wrong' if self.wrong_hash else hashlib.sha256(self.payload).hexdigest()}
        if url.endswith(':validate') and self.fail_validation:
            raise RuntimeError('Validation failed')
        return {}

    def test_upload_preserves_active_release_and_commits_draft_only(self):
        publisher.upload_draft(directory=self.directory, request=self.request)
        update = next(call[2] for call in self.calls if call[0] == 'PUT')
        self.assertEqual(update['track'], 'internal')
        self.assertEqual(update['releases'][0]['status'], 'completed')
        self.assertEqual(update['releases'][-1]['status'], 'draft')
        self.assertEqual(update['releases'][-1]['versionCodes'], ['1501'])
        self.assertTrue(self.calls[-1][1].endswith(':commit?changesNotSentForReview=true'))
        self.assertFalse(any('/tracks/production' in url for _, url, _ in self.calls))

    def test_unsigned_or_changed_candidate_never_calls_api(self):
        for changes in [{'signed': False}, {'signed': True, 'bundle_sha256': 'wrong'}]:
            self.metadata.update(changes)
            self.write_metadata()
            with self.assertRaises(ValueError):
                publisher.upload_draft(directory=self.directory, request=self.request)
            self.assertEqual(self.calls, [])

    def test_existing_draft_is_not_overwritten(self):
        self.previous = [{'status': 'draft', 'versionCodes': ['1401']}]
        with self.assertRaises(ValueError):
            publisher.upload_draft(directory=self.directory, request=self.request)
        self.assertFalse(any('uploadType=media' in url for _, url, _ in self.calls))
        self.assertEqual(self.calls[-1][0], 'DELETE')

    def test_wrong_version_and_validation_failure_never_commit(self):
        for mode in ('wrong_version', 'wrong_hash', 'fail_validation'):
            with self.subTest(mode=mode):
                self.calls = []
                self.previous = [{'name': 'Previous', 'versionCodes': ['1401'], 'status': 'completed'}]
                setattr(self, mode, True)
                with self.assertRaises((ValueError, RuntimeError)):
                    publisher.upload_draft(directory=self.directory, request=self.request)
                self.assertFalse(any(':commit' in url for _, url, _ in self.calls))
                self.assertEqual(self.calls[-1][0], 'DELETE')
                setattr(self, mode, False)


class CandidateTests(unittest.TestCase):
    def test_manifest_and_artifact_metadata_agree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / 'android/build/app-play/outputs/bundle/release/app-release.aab'
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(b'candidate')
            (root / 'server').mkdir()
            (root / 'server/CMakeLists.txt').write_text('project(hardcharger VERSION 2.11.0)')
            notes = root / 'android/play/listing/en-US/release-notes.txt'
            notes.parent.mkdir(parents=True)
            notes.write_text('Test notes')
            manifest = root / 'android/build/bundle-manifest.xml'
            template = '<manifest xmlns:android="http://schemas.android.com/apk/res/android" package="com.gizmodata.hardcharger" android:versionName="2.11.0" android:versionCode="{}"><application/></manifest>'
            environment = dict(os.environ, HARDCHARGER_ANDROID_BUILD_NUMBER='1501', SIGNED='false',
                               GITHUB_SHA='fixture-commit', GITHUB_RUN_ID='fixture-run')
            script = Path(__file__).with_name('ci-candidate.py').resolve()
            manifest.write_text(template.format(1502))
            rejected = subprocess.run(args=['python3', str(script)], cwd=root, env=environment, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((root / 'android/build/candidate').exists())
            manifest.write_text(template.format(1501))
            subprocess.run(args=['python3', str(script)], cwd=root, env=environment, check=True, capture_output=True)
            candidate = json.loads((root / 'android/build/candidate/candidate.json').read_text())
            self.assertEqual(candidate['version_code'], 1501)
            self.assertEqual(candidate['source_commit'], 'fixture-commit')
            self.assertEqual(candidate['bundle_sha256'], hashlib.sha256(b'candidate').hexdigest())
            self.assertFalse(candidate['signed'])


if __name__ == '__main__':
    unittest.main()
