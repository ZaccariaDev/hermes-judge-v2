#!/usr/bin/env python3
"""Validate in disposable copies; never patch the supplied runtime or restart it."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

BASE = '869228cab4a8276d3b4c78da9d9939670c47bd0f'


def run(argv, **kwargs):
    subprocess.run([str(x) for x in argv], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True,
                        help='Local Hermes git checkout containing the documented base commit')
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    artifact = Path(__file__).resolve().parent
    judge = artifact.parent.parent
    run(['git', '-C', runtime, 'cat-file', '-e', BASE + '^{commit}'])
    with tempfile.TemporaryDirectory(prefix='verify-hermes-fallback-') as tmp:
        core = Path(tmp) / 'core'
        run(['git', 'clone', '--quiet', '--no-checkout', '--shared', runtime, core])
        run(['git', '-C', core, 'checkout', '--quiet', BASE])
        run(['git', '-C', core, 'apply', '--check', artifact / 'changes.patch'])
        run(['git', '-C', core, 'apply', artifact / 'changes.patch'])
        home = Path(tmp) / 'profile'
        home.mkdir()
        env = dict(os.environ, HERMES_HOME=str(home),
                   PYTHONPATH=os.pathsep.join([str(judge / 'src'), str(core)]))
        for suite, pattern in ((judge / 'tests', 'test*.py'),
                               (core / 'tests/gateway', 'test_thread_fallback.py'),
                               (core / 'tests/gateway', 'test_dispatch_integration.py')):
            run([sys.executable, '-B', '-m', 'unittest', 'discover',
                 '-s', suite, '-p', pattern, '-v'], cwd=core, env=env)
    print('VERIFIED: judge + thread fallback + gateway dispatch; supplied runtime unchanged; no restart.')


if __name__ == '__main__':
    main()
