import contextlib
import io
import types
import unittest
from unittest.mock import patch

from scripts import check_tests


class StrictRunnerTests(unittest.TestCase):
    def run_with(self, tests=1, skipped=(), successful=True):
        result = types.SimpleNamespace(testsRun=tests, skipped=list(skipped), failures=[], errors=[],
                                       unexpectedSuccesses=[], wasSuccessful=lambda: successful)
        encoders = ' V..... libx264 video\n A..... libmp3lame audio\n A..... aac audio\n V..... png image'
        with patch.object(check_tests.shutil, 'which', return_value='/offline/tool'), \
                patch.object(check_tests.subprocess, 'check_output', return_value=encoders), \
                patch.object(check_tests.unittest.defaultTestLoader, 'discover', return_value=unittest.TestSuite()), \
                patch.object(check_tests.unittest.TextTestRunner, 'run', return_value=result), \
                contextlib.redirect_stdout(io.StringIO()):
            return check_tests.run()

    def test_skipped_media_is_not_a_pass(self):
        self.assertEqual(self.run_with(skipped=[('media-test', 'missing ffmpeg')]), 1)

    def test_empty_suite_is_not_a_pass(self):
        self.assertEqual(self.run_with(tests=0), 1)

    def test_failed_suite_is_not_a_pass(self):
        self.assertEqual(self.run_with(successful=False), 1)

    def test_successful_unskipped_suite_passes(self):
        self.assertEqual(self.run_with(), 0)

    def test_missing_media_tool_fails_preflight(self):
        with patch.object(check_tests.shutil, 'which', return_value=None), self.assertRaises(SystemExit):
            check_tests.run()


if __name__ == '__main__':
    unittest.main()
