"""Validate release intent before any signing credential can be accessed."""
import os
from pathlib import Path
import re


def plan(*, event, ref, sign, upload, run_number, attempt, version):
    if sign and (event == 'pull_request' or not ref.startswith(('refs/tags/v', 'refs/heads/release/'))):
        raise ValueError('Signing requires a v* tag or release/* branch, never a pull request')
    if ref.startswith('refs/tags/v') and ref != f'refs/tags/v{version}':
        raise ValueError('Release tag must match the server/app version')
    preview_push = event == 'push' and ref == 'refs/heads/release/android-preview'
    if upload and (not sign or not (event == 'workflow_dispatch' or preview_push)):
        raise ValueError('Play upload requires a signed manual run or bootstrapped preview branch')
    if run_number < 1 or not 1 <= attempt <= 99:
        raise ValueError('Invalid run number or attempt; start a new run after attempt 99')
    code = 1000 + run_number * 100 + attempt
    if code > 2100000000:
        raise ValueError('Play version-code limit exceeded')
    return {'sign': str(sign).lower(), 'code': str(code), 'upload': str(upload).lower()}


if __name__ == '__main__':
    version = re.search(r'project\(hardcharger VERSION ([0-9.]+)', Path('server/CMakeLists.txt').read_text())[1]
    result = plan(event=os.environ['GITHUB_EVENT_NAME'], ref=os.environ['GITHUB_REF'],
                  sign=os.environ['REQUEST_SIGN'] == 'true', upload=os.environ['REQUEST_UPLOAD'] == 'true',
                  run_number=int(os.environ['GITHUB_RUN_NUMBER']), attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),
                  version=version)
    with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
        output.write(''.join(f'{key}={value}\n' for key, value in result.items()))
