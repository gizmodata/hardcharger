#!/usr/bin/env python3
"""Validate store-upload PNGs and write their provenance manifest (no dependencies)."""
import hashlib
import json
from pathlib import Path
import struct
import zlib

root = Path(__file__).resolve().parents[1]
assets = []
expected = {'phone': (1080, 1920), 'tablet-7-inch': (1080, 1920), 'tablet-10-inch': (1920, 1080)}
paths = [root / 'app-icon.png', root / 'feature-graphic.png']
for group in expected:
    files = sorted((root / group).glob('*.png'))
    assert len(files) == 5, (group, len(files))
    paths.extend(files)
for file in paths:
    data = file.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n', file
    pos, compressed = 8, bytearray()
    while pos < len(data):
        size = struct.unpack('>I', data[pos:pos+4])[0]
        kind, body = data[pos+4:pos+8], data[pos+8:pos+8+size]
        checksum = struct.unpack('>I', data[pos+8+size:pos+12+size])[0]
        assert zlib.crc32(kind + body) == checksum, file
        if kind == b'IHDR':
            w, h, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', body)
        if kind == b'IDAT':
            compressed.extend(body)
        pos += size + 12
    icon = file.name == 'app-icon.png'
    dimensions = (512, 512) if icon else (1024, 500) if file.name == 'feature-graphic.png' else expected[file.parent.name]
    assert (w, h) == dimensions and depth == 8 and color == (6 if icon else 2), file
    assert compression == filtering == interlace == 0, file
    assert len(zlib.decompress(compressed)) == h * (1 + w * (4 if icon else 3)), file
    assert len(data) <= (1024 * 1024 if icon else 8 * 1024 * 1024), file
    assets.append({'file': str(file.relative_to(root)), 'width': w, 'height': h,
                   'format': 'RGBA PNG' if icon else 'RGB PNG', 'bytes': len(data),
                   'sha256': hashlib.sha256(data).hexdigest()})
manifest = {'app': 'com.gizmodata.hardcharger', 'version': '2.11.0', 'version_code': 1501,
            'distribution': 'play', 'aab_sha256': 'dd024023553094b8814613fa54daf381c80cec2a8fd623cd105192c0612737f8',
            'assets': assets}
(root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Validated {len(assets)} upload assets: dimensions, 8-bit PNG encoding, CRCs, decompression, and file-size limits.')
