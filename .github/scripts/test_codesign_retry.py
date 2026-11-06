"""Exercise transient retries and failure preservation without Apple credentials."""
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest


spec = importlib.util.spec_from_file_location("codesign_retry", Path(__file__).with_name("codesign-retry.py"))
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)


class CodesignRetryTests(unittest.TestCase):
    def invoke(self, responses, args=("--sign", "private identity", "app with spaces")):
        calls, sleeps = [], []
        stdout, stderr = io.BytesIO(), io.BytesIO()

        def run(command, **kwargs):
            calls.append(command)
            code, out, err = responses[min(len(calls) - 1, len(responses) - 1)]
            return subprocess.CompletedProcess(command, code, out, err)

        code = retry.codesign(args, run=run, sleep=sleeps.append, stdout=stdout, stderr=stderr)
        return code, calls, sleeps, stdout.getvalue(), stderr.getvalue()

    def test_transient_failure_recovers_and_preserves_arguments_and_output(self):
        failure = (1, b"", b"app: The timestamp service is not available.\n")
        code, calls, sleeps, out, err = self.invoke([failure, failure, (0, b"signed\n", b"")])
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [10, 20])
        self.assertEqual(calls, [["/usr/bin/codesign", "--sign", "private identity", "app with spaces"]] * 3)
        self.assertEqual(out, b"signed\n")
        self.assertNotIn(b"private identity", err)

    def test_exhaustion_is_bounded_and_keeps_failure(self):
        code, calls, sleeps, _, _ = self.invoke([(7, b"", retry.TIMESTAMP_UNAVAILABLE)])
        self.assertEqual(code, 7)
        self.assertEqual(len(calls), 6)
        self.assertEqual(sleeps, [10, 20, 40, 60, 60])

    def test_certificate_and_entitlement_errors_are_not_retried(self):
        for error in retry.PERMANENT_ERRORS:
            with self.subTest(error=error):
                code, calls, sleeps, _, _ = self.invoke([(1, b"", retry.TIMESTAMP_UNAVAILABLE + b"\n" + error)])
                self.assertEqual((code, len(calls), sleeps), (1, 1, []))

    def test_unknown_errors_are_not_retried(self):
        code, calls, sleeps, _, err = self.invoke([(9, b"", b"unknown failure\n")])
        self.assertEqual((code, len(calls), sleeps), (9, 1, []))
        self.assertEqual(err, b"unknown failure\n")

    def test_verification_and_notarization_rejections_are_not_retried(self):
        for args in (("--verify", "--strict", "app"), ("-v", "--sign", "identity", "app"),
                     ("--check-notarization", "app")):
            with self.subTest(args=args):
                code, calls, sleeps, _, _ = self.invoke([(1, b"", retry.TIMESTAMP_UNAVAILABLE)], args=args)
                self.assertEqual((code, len(calls), sleeps), (1, 1, []))

    def test_success_is_never_retried(self):
        code, calls, sleeps, _, _ = self.invoke([(0, b"", retry.TIMESTAMP_UNAVAILABLE)])
        self.assertEqual((code, len(calls), sleeps), (0, 1, []))


if __name__ == "__main__":
    unittest.main()
