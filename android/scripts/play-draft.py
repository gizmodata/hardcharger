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
        upload_url = edit_url.replace('/androidpublisher/v3/', '/upload/androidpublisher/v3/')
        result = request(method='POST', url=f'{upload_url}/bundles?uploadType=media', body=data)
        if int(result['versionCode']) != code or result['sha256'].lower() != metadata['bundle_sha256']:
            raise ValueError('Google returned an unexpected bundle version or checksum')
        releases.append({'name': f"{metadata['version']} ({code})", 'versionCodes': [str(code)],
                         'status': 'draft', 'releaseNotes': [{'language': 'en-US', 'text': notes}]})
        request(method='PUT', url=f'{edit_url}/tracks/internal', body={'track': 'internal', 'releases': releases})
        request(method='POST', url=f'{edit_url}:validate')
        request(method='POST', url=f'{edit_url}:commit?changesNotSentForReview=true')
    except Exception:
        # No fallback to a completed release, production, or sending changes for review.
        try:
            request(method='DELETE', url=edit_url)
        except Exception:
            pass
        raise


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
        raise RuntimeError(f'Play API {error.code}: {detail}') from None


if __name__ == '__main__':
    upload_draft(directory=Path(sys.argv[1]), request=api_request)
    print('Uploaded an internal-testing draft. No rollout or review submission requested.')
