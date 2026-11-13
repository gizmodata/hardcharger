"""Allow upload-only retries of candidates whose original build gates passed."""
import hashlib
import json
import os
from pathlib import Path
import urllib.request

REPOSITORY = 'gizmodata/hardcharger'
BRANCH = 'release/android-preview'


def verify_source(*, metadata, run, jobs, run_id, ref, payload):
    if ref != f'refs/heads/{BRANCH}' or not str(run_id).isdigit():
        raise ValueError('Candidate retry requires the Android preview branch and a numeric run ID')
    if (str(run['id']) != str(run_id) or run['repository']['full_name'] != REPOSITORY
            or run['repository']['id'] != 1422890225 or run['head_branch'] != BRANCH
            or run['path'] != '.github/workflows/android.yml'
            or run['event'] not in ('push', 'workflow_dispatch')):
        raise ValueError('Candidate did not originate in the trusted Android preview workflow')
    passed = {j['name'] for j in jobs if j['status'] == 'completed' and j['conclusion'] == 'success'}
    if not {'plan', 'ui', 'native (arm64-v8a)', 'native (x86_64)', 'bundle'} <= passed:
        raise ValueError('Original candidate build/test/signing gates did not all pass')
    if (metadata['package'] != 'com.gizmodata.hardcharger' or metadata['signed'] is not True
            or str(metadata['run_id']) != str(run_id) or metadata['source_commit'] != run['head_sha']
            or metadata['bundle_sha256'] != hashlib.sha256(payload).hexdigest()):
        raise ValueError('Candidate identity, signature status, source or checksum mismatch')


def github_json(path):
    request = urllib.request.Request(
        url=f'https://api.github.com/repos/{REPOSITORY}/{path}',
        headers={'Authorization': f"Bearer {os.environ['GITHUB_TOKEN']}",
                 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


if __name__ == '__main__':
    run_id = os.environ['SOURCE_RUN_ID']
    if not run_id.isdigit():
        raise SystemExit('Source run ID must be numeric')
    run = github_json(path=f'actions/runs/{run_id}')
    jobs = []
    page = 1
    while True:
        batch = github_json(path=f'actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}')['jobs']
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    directory = Path('candidate')
    metadata = json.loads((directory / 'candidate.json').read_text())
    verify_source(metadata=metadata, run=run, jobs=jobs, run_id=run_id,
                  ref=os.environ['GITHUB_REF'], payload=(directory / 'hardcharger-play.aab').read_bytes())
    print(f"Verified original gates and candidate {metadata['version_code']} from {metadata['source_commit']}")
