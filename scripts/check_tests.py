import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run():
    for tool in ('ffmpeg', 'ffprobe'):
        if not shutil.which(tool):
            raise SystemExit(f'{tool} is required; media tests must not be skipped')
    encoders = subprocess.check_output(['ffmpeg', '-hide_banner', '-encoders'], text=True)
    for encoder in ('libx264', 'libmp3lame', 'aac', 'png'):
        if not any(encoder == line.split()[1] for line in encoders.splitlines() if len(line.split()) > 1):
            raise SystemExit('Required FFmpeg encoder missing: ' + encoder)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'unexpected_successes': len(result.unexpectedSuccesses)}
    print(json.dumps(summary), flush=True)
    return 0 if result.testsRun and result.wasSuccessful() and not result.skipped else 1


if __name__ == '__main__':
    raise SystemExit(run())
