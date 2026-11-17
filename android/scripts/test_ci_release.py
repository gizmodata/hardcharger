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
publisher = load(name='play-internal')
source_verifier = load(name='ci-verify-upload-source')


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

    def test_combined_release_uses_server_run_counter(self):
        result = self.plan(ref='refs/tags/v2.12.0', version='2.12.0', run_number=322)
        self.assertEqual(result, {'sign': 'true', 'code': '33201', 'upload': 'false'})

    def test_run_and_retry_codes_increase_without_collision(self):
        self.assertLess(int(self.plan()['code']), int(self.plan(attempt=2)['code']))
        self.assertLess(int(self.plan(attempt=99)['code']), int(self.plan(run_number=6)['code']))
        for changes in [dict(attempt=100), dict(run_number=0), dict(run_number=21000000)]:
            with self.assertRaises(ValueError):
                self.plan(**changes)


class InternalPublishTests(unittest.TestCase):
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
        self.internal = [{'name': 'Previous', 'versionCodes': ['1401'], 'status': 'completed'}]
        self.production = [{'name': 'Store', 'versionCodes': ['1301'], 'status': 'completed'}]
        self.fail_validation = False
        self.wrong_version = False
        self.wrong_hash = False
        self.existing_bundles = []
        self.commit_error = None

    def write_metadata(self):
        (self.directory / 'candidate.json').write_text(json.dumps(self.metadata))

    def request(self, *, method, url, body=None):
        self.calls.append((method, url, body))
        if url.endswith('/edits'):
            return {'id': 'edit-1'}
        if method == 'GET' and url.endswith('/bundles'):
            return {'bundles': self.existing_bundles}
        if method == 'GET' and url.endswith('/tracks/internal'):
            return {'releases': self.internal}
        if method == 'GET' and url.endswith('/tracks/production'):
            return {'releases': self.production}
        if method == 'GET':
            raise AssertionError(f'unexpected read {url}')
        if 'uploadType=media' in url:
            return {'versionCode': 1502 if self.wrong_version else 1501,
                    'sha256': 'wrong' if self.wrong_hash else hashlib.sha256(self.payload).hexdigest()}
        if url.endswith(':validate') and self.fail_validation:
            raise RuntimeError('Validation failed')
        if ':commit' in url and 'changesNotSentForReview=true' in url and self.commit_error:
            raise self.commit_error
        return {}

    def puts(self):
        return [(url, body) for method, url, body in self.calls if method == 'PUT']

    def test_publishes_a_completed_internal_release_and_touches_no_other_track(self):
        publisher.publish_internal(directory=self.directory, request=self.request)
        [(url, update)] = self.puts()
        self.assertTrue(url.endswith('/tracks/internal'))
        self.assertEqual(update, {'track': 'internal', 'releases': [
            {'name': '2.11.0 (1501)', 'versionCodes': ['1501'], 'status': 'completed',
             'releaseNotes': [{'language': 'en-US', 'text': 'Internal test release'}]}]})
        self.assertTrue(self.calls[-1][1].endswith(':commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW&changesNotSentForReview=true'))
        for method, url, _ in self.calls:
            if '/tracks/' in url and not url.endswith('/tracks/internal'):
                self.assertEqual(method, 'GET', url)

    def test_leftover_draft_no_longer_blocks_and_is_replaced(self):
        # 2026-11-15: the 2.13.0 draft blocked the 2.13.1 upload under the old draft-only rule.
        self.internal = [{'status': 'completed', 'versionCodes': ['1401']},
                         {'status': 'draft', 'versionCodes': ['1451']}]
        expected = publisher.publish_internal(directory=self.directory, request=self.request)
        [(_, update)] = self.puts()
        self.assertEqual([(r['status'], r['versionCodes']) for r in update['releases']], [('completed', ['1501'])])
        self.assertEqual(expected['replaced'], self.internal)
        self.assertTrue(any(':commit' in url for _, url, _ in self.calls))

    def test_draft_with_a_higher_code_does_not_block(self):
        self.internal = [{'status': 'draft', 'versionCodes': ['9901']}]
        publisher.publish_internal(directory=self.directory, request=self.request)
        self.assertEqual(len(self.puts()), 1)

    def test_never_moves_testers_to_an_older_or_same_build(self):
        for code in ('1501', '1601'):
            with self.subTest(live=code):
                self.calls = []
                self.internal = [{'status': 'completed', 'versionCodes': [code]}]
                with self.assertRaisesRegex(ValueError, 'refusing to publish older build 1501'):
                    publisher.publish_internal(directory=self.directory, request=self.request)
                self.assertFalse(any('uploadType=media' in url or ':commit' in url for _, url, _ in self.calls))
                self.assertEqual(self.calls[-1][0], 'DELETE')

    def test_only_specific_google_flag_error_retries_commit_without_flag(self):
        self.commit_error = publisher.PlayApiError(status=400, message=publisher.AUTOMATIC_REVIEW_MESSAGE)
        publisher.publish_internal(directory=self.directory, request=self.request)
        commits = [url for method, url, _ in self.calls if ':commit' in url]
        self.assertEqual(len(commits), 2)
        self.assertTrue(commits[-1].endswith(':commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW'))

    def test_other_commit_errors_abort_without_retry(self):
        self.commit_error = publisher.PlayApiError(status=400, message='Changes already in review')
        with self.assertRaises(publisher.PlayApiError):
            publisher.publish_internal(directory=self.directory, request=self.request)
        self.assertEqual(sum(':commit' in url for _, url, _ in self.calls), 1)
        self.assertEqual(self.calls[-1][0], 'DELETE')

    def test_reuses_only_matching_previously_uploaded_bundle(self):
        self.existing_bundles = [{'versionCode': 1501, 'sha256': self.metadata['bundle_sha256']}]
        publisher.publish_internal(directory=self.directory, request=self.request)
        self.assertFalse(any('uploadType=media' in url for _, url, _ in self.calls))
        self.calls = []
        self.existing_bundles[0]['sha256'] = 'different'
        with self.assertRaises(ValueError):
            publisher.publish_internal(directory=self.directory, request=self.request)
        self.assertFalse(any(method == 'PUT' or ':commit' in url for method, url, _ in self.calls))

    def test_readback_requires_the_build_completed_and_production_unchanged(self):
        expected = {'version_code': 1501, 'replaced': list(self.internal), 'production': list(self.production)}
        self.internal = [{'status': 'completed', 'versionCodes': ['1501']}]
        self.existing_bundles = [{'versionCode': 1501, 'sha256': self.metadata['bundle_sha256']}]
        publisher.verify_published(directory=self.directory, expected=expected, request=self.request)
        self.assertEqual(self.calls[-1][0], 'DELETE')
        for internal in ([{'status': 'draft', 'versionCodes': ['1501']}],
                         [{'status': 'completed', 'versionCodes': ['1401']}],
                         [{'status': 'completed', 'versionCodes': ['1501']}, {'status': 'draft', 'versionCodes': ['1451']}]):
            with self.subTest(internal=internal):
                self.internal = internal
                with self.assertRaisesRegex(ValueError, 'Internal testing does not show build 1501'):
                    publisher.verify_published(directory=self.directory, expected=expected, request=self.request)
        self.internal = [{'status': 'completed', 'versionCodes': ['1501']}]
        self.production = [{'status': 'completed', 'versionCodes': ['1501']}]
        with self.assertRaisesRegex(ValueError, 'production track changed'):
            publisher.verify_published(directory=self.directory, expected=expected, request=self.request)
        self.production = expected['production']
        self.existing_bundles[0]['sha256'] = 'different'
        with self.assertRaisesRegex(ValueError, 'checksum does not match'):
            publisher.verify_published(directory=self.directory, expected=expected, request=self.request)

    def test_unsigned_or_changed_candidate_never_calls_api(self):
        for changes in [{'signed': False}, {'signed': True, 'bundle_sha256': 'wrong'}]:
            self.metadata.update(changes)
            self.write_metadata()
            with self.assertRaises(ValueError):
                publisher.publish_internal(directory=self.directory, request=self.request)
            self.assertEqual(self.calls, [])

    def test_wrong_version_and_validation_failure_never_commit(self):
        for mode in ('wrong_version', 'wrong_hash', 'fail_validation'):
            with self.subTest(mode=mode):
                self.calls = []
                setattr(self, mode, True)
                with self.assertRaises((ValueError, RuntimeError)):
                    publisher.publish_internal(directory=self.directory, request=self.request)
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


class UploadSourceTests(unittest.TestCase):
    def setUp(self):
        self.payload = b'original signed candidate'
        self.metadata = {'package': 'com.gizmodata.hardcharger', 'signed': True,
                         'version': '2.12.0', 'run_id': '123', 'source_commit': 'original-commit',
                         'bundle_sha256': hashlib.sha256(self.payload).hexdigest()}
        self.run = {'id': 123, 'repository': {'full_name': 'gizmodata/hardcharger', 'id': 1422890225},
                    'head_branch': 'v2.12.0', 'path': '.github/workflows/server.yml',
                    'event': 'push', 'head_sha': 'original-commit', 'conclusion': 'failure'}
        self.jobs = [{'name': n, 'status': 'completed', 'conclusion': 'success'}
                     for n in ('android / plan', 'web UI (build)', 'android / native (arm64-v8a)', 'android / native (x86_64)', 'android / bundle', 'release')]
        self.jobs.append({'name': 'android-play', 'status': 'completed', 'conclusion': 'failure'})

    def verify(self, ref='refs/tags/v2.12.0'):
        source_verifier.verify_source(metadata=self.metadata, run=self.run, jobs=self.jobs,
                                     run_id='123', ref=ref, payload=self.payload)

    def test_publish_only_tag_of_the_same_version_is_accepted(self):
        for ref in ('refs/tags/v2.12.0-play.1', 'refs/tags/v2.12.0-play.12'):
            with self.subTest(ref=ref):
                self.verify(ref=ref)
        for ref in ('refs/tags/v2.12.1', 'refs/tags/v2.12.00', 'refs/tags/v2.12.0-play.', 'refs/tags/v2.12.0-play.0',
                    'refs/tags/v2.12.0-play.1x', 'refs/tags/v2.12.0-rc.1', 'refs/heads/v2.12.0', 'refs/heads/main',
                    'refs/tags/v2.12.0-play.1000', 'refs/tags/v2x12x0'):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, 'this version'):
                self.verify(ref=ref)
        # The candidate must still come from the version's own release tag run.
        self.run['head_branch'] = 'v2.12.0-play.1'
        with self.assertRaisesRegex(ValueError, 'this version'):
            self.verify(ref='refs/tags/v2.12.0-play.1')

    def test_upload_failure_can_reuse_successful_build(self):
        self.verify()

    def test_wrong_source_or_event_is_rejected(self):
        for key, value in [('head_branch', 'main'), ('event', 'pull_request'), ('head_sha', 'different'),
                           ('path', '.github/workflows/untrusted.yml'), ('id', 456)]:
            with self.subTest(key=key):
                previous = self.run[key]
                self.run[key] = value
                with self.assertRaises(ValueError):
                    self.verify()
                self.run[key] = previous

    def test_failed_build_gate_or_modified_artifact_is_rejected(self):
        self.jobs[2]['conclusion'] = 'failure'
        with self.assertRaises(ValueError):
            self.verify()
        self.jobs[2]['conclusion'] = 'success'
        self.payload = b'tampered'
        with self.assertRaises(ValueError):
            self.verify()


if __name__ == '__main__':
    unittest.main()
