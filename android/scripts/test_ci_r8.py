import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('r8_check', Path(__file__).with_name('check-r8-bundle.py'))
r8_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r8_check)


class R8BundleTest(unittest.TestCase):
    def bundle(self, *, enabled=True, excluded=3, mapping=True, renamed=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'test.aab'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('BUNDLE-METADATA/com.android.tools/r8.json', json.dumps({
                'options': {key: enabled for key in ('isObfuscationEnabled', 'isOptimizationsEnabled', 'isShrinkingEnabled')},
                'stats': {key: excluded for key in ('noObfuscationPercentage', 'noOptimizationPercentage', 'noShrinkingPercentage')},
                'version': 'test'}))
            if mapping:
                name = 'com.gizmodata.hardcharger.NativeBridge'
                archive.writestr('BUNDLE-METADATA/com.android.tools.build.obfuscation/proguard.map',
                                 f'{name} -> {"a.b" if renamed else name}:\n')
            archive.writestr('base/dex/classes.dex', b'dex')
        return path

    def test_accepts_optimized_bundle_and_reports_coverage(self):
        result = r8_check.check(bundle=self.bundle())
        self.assertEqual(result['coverage_percent']['obfuscation'], 97)
        self.assertEqual(result['dex_bytes'], 3)

    def test_rejects_disabled_optimization_or_excessive_keep_rules(self):
        for options in ({'enabled': False}, {'excluded': 76}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                r8_check.check(bundle=self.bundle(**options))

    def test_rejects_missing_mapping_and_renamed_jni_class(self):
        with self.assertRaises(KeyError):
            r8_check.check(bundle=self.bundle(mapping=False))
        with self.assertRaises(ValueError):
            r8_check.check(bundle=self.bundle(renamed=True))
