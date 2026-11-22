import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('ci-release-tests.sh').resolve()
FAKE_ADB = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> adb-calls
case "$*" in
  'shell am instrument -w '*)
    if [ "$ADB_TEST_MODE" = assertion-failure ]; then
      echo 'FAILURES!!!'
    else
      echo 'OK (1 test)'
    fi
    ;;
  'pull '*)
    if [[ "$ADB_TEST_MODE" = reconnect && "$3" = */1280x600-2.0/ && ! -e pull-failed ]]; then
      touch pull-failed
      echo 'adb: device offline' >&2
      exit 1
    fi
    touch "$3/screenshot.png"
    ;;
  'shell wm size 1280x400')
    if [ "$ADB_TEST_MODE" = reconnect ] && [ ! -e setup-failed ]; then
      touch setup-failed
      echo "adb: device 'emulator-5554' not found" >&2
      exit 1
    fi
    ;;
  'shell wm density 160')
    case "$ADB_TEST_MODE" in
      offline) echo 'adb: device offline' >&2; exit 7 ;;
      setup-error) echo 'adb: permission denied' >&2; exit 8 ;;
    esac
    ;;
esac
'''


class ReleaseUiTests(unittest.TestCase):
    def run_release(self, *, mode):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        (directory / 'android/build/ci').mkdir(parents=True)
        adb = directory / 'adb'
        adb.write_text(FAKE_ADB)
        adb.chmod(0o755)
        env = dict(os.environ, GITHUB_ACTIONS='true', ADB_TEST_MODE=mode,
                   PATH=f'{directory}{os.pathsep}{os.environ["PATH"]}')
        result = subprocess.run(args=['bash', str(SCRIPT)], cwd=directory, env=env,
                                capture_output=True, text=True, timeout=10)
        calls = (directory / 'adb-calls').read_text().splitlines()
        return result, calls, directory

    def test_transient_evidence_and_setup_disconnects_still_run_all_scenarios(self):
        result, calls, directory = self.run_release(mode='reconnect')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('reconnect offline'), 2)
        self.assertEqual(calls.count('wait-for-device'), 2)
        self.assertEqual(sum(call.startswith('shell am instrument ') for call in calls), 4)
        screenshots = (directory / 'android/build/ci/release-evidence').glob('*/screenshot.png')
        self.assertEqual({path.parent.name for path in screenshots},
                         {'phone', '1280x600-1.0', '1280x600-2.0', '1280x400-2.0'})

    def test_setup_does_not_retry_non_transport_errors_and_bounds_offline_retries(self):
        for mode, expected_status, attempts in [('setup-error', 8, 1), ('offline', 7, 3)]:
            with self.subTest(mode=mode):
                result, calls, _ = self.run_release(mode=mode)
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual(calls.count('shell wm density 160'), attempts)
                self.assertEqual(sum(call.startswith('shell am instrument ') for call in calls), 1)

    def test_failed_instrumentation_still_fails_without_retry(self):
        result, calls, _ = self.run_release(mode='assertion-failure')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('FAILURES!!!', result.stdout)
        self.assertEqual(sum(call.startswith('shell am instrument ') for call in calls), 1)
        self.assertIn('logcat -d -b crash', calls)
