#!/usr/bin/env python3
"""Require actual R8 optimization metadata and crash mappings in the upload bundle."""
import json
from pathlib import Path
import sys
import zipfile


def check(*, bundle):
    with zipfile.ZipFile(bundle) as archive:
        metadata = json.loads(archive.read('BUNDLE-METADATA/com.android.tools/r8.json'))
        mapping = archive.read('BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map').decode()
        dex_bytes = sum(info.file_size for info in archive.infolist() if info.filename.endswith('.dex'))
    options = metadata['options']
    for option in ('isObfuscationEnabled', 'isOptimizationsEnabled', 'isShrinkingEnabled'):
        if options.get(option) is not True:
            raise ValueError(f'R8 must enable {option}')
    scores = {}
    for label, key in (('obfuscation', 'noObfuscationPercentage'),
                       ('optimization', 'noOptimizationPercentage'), ('shrinking', 'noShrinkingPercentage')):
        excluded = metadata['stats'][key]
        if not isinstance(excluded, (int, float)) or not 0 <= excluded <= 75:
            raise ValueError(f'R8 {label} must cover at least 25%: excluded={excluded}')
        scores[label] = round(100 - excluded, 2)
    boundary = 'com.gizmodata.hardcharger.NativeBridge'
    if f'{boundary} -> {boundary}:' not in mapping:
        raise ValueError('JNI class name was removed or obfuscated')
    return {'dex_bytes': dex_bytes, 'r8_version': metadata['version'],
            'mapping_bytes': len(mapping.encode()), 'coverage_percent': scores}


if __name__ == '__main__':
    print(json.dumps(check(bundle=Path(sys.argv[1])), indent=2))
