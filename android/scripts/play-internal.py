"""Publish one verified CI candidate to Google Play's internal testing track.

Matches the iOS TestFlight upload: every release build reaches internal testers without a
Console step. The candidate replaces whatever the internal track held (an older completed
release, or a draft left from an earlier upload). No other track is ever touched: closed and
open testing and production stay manual in Play Console.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

PACKAGE = 'com.gizmodata.hardcharger'
BASE = f'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PACKAGE}'
TRACK = 'internal'
AUTOMATIC_REVIEW_MESSAGE = ('Changes are sent for review automatically. '
                            'The query parameter changesNotSentForReview must not be set.')


class PlayApiError(RuntimeError):
    def __init__(self, *, status, message):
        self.status = status
        self.message = message
        super().__init__(f'Play API {status}: {message}')


def release_codes(*, releases):
    return {int(code) for release in releases for code in release.get('versionCodes', [])}


def track_state(*, releases):
    return sorted((tuple(sorted(release.get('versionCodes', []))), release.get('status', ''))
                  for release in releases)


def read_candidate(*, directory):
    metadata = json.loads((directory / 'candidate.json').read_text())
    data = (directory / 'hardcharger-play.aab').read_bytes()
    if metadata['package'] != PACKAGE or metadata['signed'] is not True:
        raise ValueError('Expected a signed Hardcharger candidate')
    if hashlib.sha256(data).hexdigest() != metadata['bundle_sha256']:
        raise ValueError('Candidate checksum mismatch')
    code = metadata['version_code']
    if type(code) is not int or not 0 < code <= 2100000000:
        raise ValueError('Invalid version code')
    notes = (directory / 'release-notes.txt').read_text().strip()
    if not notes or len(notes) > 500:
        raise ValueError('Release notes must contain 1–500 characters')
    return metadata, data, notes


def open_edit(*, request):
    edit = request(method='POST', url=f'{BASE}/edits', body={})['id']
    return f'{BASE}/edits/{urllib.parse.quote(edit, safe="")}'


def publish_internal(*, directory, request):
    metadata, data, notes = read_candidate(directory=directory)
    code = metadata['version_code']
    edit_url = open_edit(request=request)
    try:
        replaced = request(method='GET', url=f'{edit_url}/tracks/{TRACK}').get('releases', [])
        production = request(method='GET', url=f'{edit_url}/tracks/production').get('releases', [])
        # Never move internal testers back to an older build (a retry of an old candidate
        # after a newer release published). Drafts don't count: testers never got them.
        live = [r for r in replaced if r.get('status') != 'draft']
        newer = [c for c in release_codes(releases=live) if c >= code]
        if newer:
            raise ValueError(f'Internal testing already has build {max(newer)}; refusing to publish older build {code}')
        # Uploaded bundles can survive an abandoned edit. Reuse only the exact bytes.
        bundles = request(method='GET', url=f'{edit_url}/bundles').get('bundles', [])
        result = next((b for b in bundles if int(b['versionCode']) == code), None)
        if result is None:
            upload_url = edit_url.replace('/androidpublisher/v3/', '/upload/androidpublisher/v3/')
            result = request(method='POST', url=f'{upload_url}/bundles?uploadType=media', body=data)
        if int(result['versionCode']) != code or result['sha256'].lower() != metadata['bundle_sha256']:
            raise ValueError('Google returned an unexpected bundle version or checksum')
        release = {'name': f"{metadata['version']} ({code})", 'versionCodes': [str(code)],
                   'status': 'completed', 'releaseNotes': [{'language': 'en-US', 'text': notes}]}
        # The new release is the track's only release: it supersedes the previous completed
        # release and any leftover draft, as a new TestFlight build does.
        request(method='PUT', url=f'{edit_url}/tracks/{TRACK}', body={'track': TRACK, 'releases': [release]})
        request(method='POST', url=f'{edit_url}:validate')
        commit_url = f'{edit_url}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW'
        try:
            request(method='POST', url=f'{commit_url}&changesNotSentForReview=true')
        except PlayApiError as error:
            if error.status != 400 or error.message != AUTOMATIC_REVIEW_MESSAGE:
                raise
            # Google rejects the optional flag for this app/track state; the edit is unchanged.
            request(method='POST', url=commit_url)
    except Exception:
        try:
            request(method='DELETE', url=edit_url)
        except Exception:
            pass
        raise
    return {'version_code': code, 'replaced': replaced, 'production': production}


def verify_published(*, directory, expected, request):
    metadata = json.loads((directory / 'candidate.json').read_text())
    code = str(expected['version_code'])
    edit_url = open_edit(request=request)
    try:
        releases = request(method='GET', url=f'{edit_url}/tracks/{TRACK}').get('releases', [])
        if track_state(releases=releases) != [((code,), 'completed')]:
            raise ValueError(f'Internal testing does not show build {code} as its only, completed release: '
                             f'{track_state(releases=releases)}; inspect Console')
        production = request(method='GET', url=f'{edit_url}/tracks/production').get('releases', [])
        if track_state(releases=production) != track_state(releases=expected['production']):
            raise ValueError('The production track changed during the upload; inspect Console')
        bundles = request(method='GET', url=f'{edit_url}/bundles').get('bundles', [])
        if not any(str(b['versionCode']) == code and b['sha256'].lower() == metadata['bundle_sha256'] for b in bundles):
            raise ValueError('Published bundle checksum does not match the candidate')
        replaced = track_state(releases=expected['replaced'])
        print(f'Verified: build {code} is the completed internal testing release, its bundle SHA-256 matches, '
              f'production is unchanged. Replaced: {replaced or "nothing"}.')
    finally:
        # This second edit only reads committed state and is never committed itself.
        request(method='DELETE', url=edit_url)


def api_request(*, method, url, body=None):
    token = os.environ['GOOGLE_PLAY_ACCESS_TOKEN']
    if not token:
        raise ValueError('Missing Google Play access token')
    binary = isinstance(body, bytes)
    payload = body if binary else (json.dumps(body).encode() if body is not None else None)
    headers = {'Authorization': f'Bearer {token}',
               'Content-Type': 'application/octet-stream' if binary else 'application/json'}
    call = urllib.request.Request(url=url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(call, timeout=600) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors='replace').replace(token, '[redacted]')
        try:
            message = json.loads(detail)['error']['message']
        except (ValueError, KeyError, TypeError):
            message = detail
        raise PlayApiError(status=error.code, message=message) from None


if __name__ == '__main__':
    directory = Path(sys.argv[1])
    expected = publish_internal(directory=directory, request=api_request)
    verify_published(directory=directory, expected=expected, request=api_request)
    print('Published to internal testing. Closed/open testing and production are unchanged.')
