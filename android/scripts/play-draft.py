"""Upload one verified CI candidate to the internal track as a draft, never roll out."""
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
AUTOMATIC_REVIEW_MESSAGE = ('Changes are sent for review automatically. '
                            'The query parameter changesNotSentForReview must not be set.')


class PlayApiError(RuntimeError):
    def __init__(self, *, status, message):
        self.status = status
        self.message = message
        super().__init__(f'Play API {status}: {message}')


def upload_draft(*, directory, request):
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
    edit = request(method='POST', url=f'{BASE}/edits', body={})['id']
    edit_url = f'{BASE}/edits/{urllib.parse.quote(edit, safe="")}'
    try:
        track = request(method='GET', url=f'{edit_url}/tracks/internal')
        releases = track.get('releases', [])
        if any(release['status'] == 'draft' for release in releases):
            raise ValueError('An internal draft already exists; review it in Console before uploading another')
        # Uploaded bundles can survive an abandoned edit. Reuse only the exact bytes.
        bundles = request(method='GET', url=f'{edit_url}/bundles').get('bundles', [])
        result = next((b for b in bundles if int(b['versionCode']) == code), None)
        if result is None:
            upload_url = edit_url.replace('/androidpublisher/v3/', '/upload/androidpublisher/v3/')
            result = request(method='POST', url=f'{upload_url}/bundles?uploadType=media', body=data)
        if int(result['versionCode']) != code or result['sha256'].lower() != metadata['bundle_sha256']:
            raise ValueError('Google returned an unexpected bundle version or checksum')
        releases.append({'name': f"{metadata['version']} ({code})", 'versionCodes': [str(code)],
                         'status': 'draft', 'releaseNotes': [{'language': 'en-US', 'text': notes}]})
        request(method='PUT', url=f'{edit_url}/tracks/internal', body={'track': 'internal', 'releases': releases})
        request(method='POST', url=f'{edit_url}:validate')
        commit_url = f'{edit_url}:commit?changesInReviewBehavior=ERROR_IF_IN_REVIEW'
        try:
            request(method='POST', url=f'{commit_url}&changesNotSentForReview=true')
        except PlayApiError as error:
            if error.status != 400 or error.message != AUTOMATIC_REVIEW_MESSAGE:
                raise
            # Google rejects the optional flag for this app/track state. The only
            # release we added is still an internal draft; never promote its status.
            request(method='POST', url=commit_url)
    except Exception:
        # Never fall back to a completed release or production.
        try:
            request(method='DELETE', url=edit_url)
        except Exception:
            pass
        raise
    return {'version_code': code, 'previous_releases': releases[:-1]}


def verify_saved_draft(*, directory, expected, request):
    metadata = json.loads((directory / 'candidate.json').read_text())
    edit = request(method='POST', url=f'{BASE}/edits', body={})['id']
    edit_url = f'{BASE}/edits/{urllib.parse.quote(edit, safe="")}'
    try:
        releases = request(method='GET', url=f'{edit_url}/tracks/internal').get('releases', [])
        code = str(expected['version_code'])
        matches = [r for r in releases if code in r.get('versionCodes', [])]
        if len(matches) != 1 or matches[0]['status'] != 'draft':
            raise ValueError('Committed candidate is not an internal draft; inspect Console before retrying')
        current = {(tuple(sorted(r.get('versionCodes', []))), r['status']) for r in releases}
        previous = {(tuple(sorted(r.get('versionCodes', []))), r['status']) for r in expected['previous_releases']}
        if not previous <= current:
            raise ValueError('Previous internal release status changed; inspect Console before retrying')
        bundles = request(method='GET', url=f'{edit_url}/bundles').get('bundles', [])
        if not any(str(b['versionCode']) == code and b['sha256'].lower() == metadata['bundle_sha256'] for b in bundles):
            raise ValueError('Committed bundle checksum does not match the candidate')
        print(f'Verified saved internal draft {code}, matching bundle SHA-256, and retained previous releases.')
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
    expected = upload_draft(directory=directory, request=api_request)
    verify_saved_draft(directory=directory, expected=expected, request=api_request)
    print('Saved an internal-testing draft. No rollout requested; existing releases retained.')
