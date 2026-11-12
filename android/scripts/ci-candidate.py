"""Keep an exact CI artifact, its checksum, build number and source commit together."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

source = Path('android/build/app-play/outputs/bundle/release/app-release.aab')
version = re.search(r'project\(hardcharger VERSION ([0-9.]+)', Path('server/CMakeLists.txt').read_text())[1]
code = int(os.environ['HARDCHARGER_ANDROID_BUILD_NUMBER'])
manifest = ET.parse('android/build/bundle-manifest.xml').getroot()
namespace = '{http://schemas.android.com/apk/res/android}'
if (manifest.get('package') != 'com.gizmodata.hardcharger' or
        manifest.get(namespace + 'versionCode') != str(code) or
        manifest.get(namespace + 'versionName') != version or
        manifest.find('application').get(namespace + 'debuggable', 'false') != 'false'):
    raise SystemExit('The built release manifest does not match this candidate')
directory = Path('android/build/candidate')
directory.mkdir(parents=True, exist_ok=True)
bundle = directory / 'hardcharger-play.aab'
shutil.copy2(source, bundle)
metadata = {
    'package': 'com.gizmodata.hardcharger',
    'version': version,
    'version_code': code,
    'source_commit': os.environ['GITHUB_SHA'],
    'run_id': os.environ['GITHUB_RUN_ID'],
    'signed': os.environ['SIGNED'] == 'true',
    'bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
}
(directory / 'candidate.json').write_text(json.dumps(metadata, indent=2) + '\n')
(directory / 'SHA256SUMS').write_text(f"{metadata['bundle_sha256']}  {bundle.name}\n")
shutil.copy2('android/play/listing/en-US/release-notes.txt', directory / 'release-notes.txt')
