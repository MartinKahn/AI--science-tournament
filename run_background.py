"""Launch or supervise a resumable tournament independently of an interactive PTY.
Credentials are inherited in memory, never written to disk or command arguments.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent

def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--entrypoint',choices=['tournament.py','tournament_v6.py','tournament_v7.py'],default='tournament.py')
    parser.add_argument('--config',type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    config_path=(args.config or ROOT/('config.v7.json' if args.entrypoint=='tournament_v7.py' else 'config.v6.json' if args.entrypoint=='tournament_v6.py' else 'config.json')).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    if not args.worker:
        config = json.loads(config_path.read_text())
        if not config.get('allow_paid_run'):
            parser.error('Paid execution is disabled')
        if not os.environ.get(config['api_key_env']):
            parser.error('API credential is not configured')
        status_path = run_dir/'background-status.json'
        if status_path.exists():
            previous = json.loads(status_path.read_text())
            if previous.get('status') == 'running':
                try:
                    os.kill(previous['supervisor_pid'], 0)
                except ProcessLookupError:
                    pass
                else:
                    parser.error('A background supervisor is already running')
        with (run_dir/'background.log').open('a') as log:
            process = subprocess.Popen(
                [sys.executable, '-u', str(Path(__file__).resolve()),
                 '--worker', '--run-dir', str(run_dir),'--entrypoint',args.entrypoint,'--config',str(config_path)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True)
        print('Detached supervisor PID:', process.pid, flush=True)
        return
    command = [sys.executable, '-u', str(ROOT/args.entrypoint),
               'run', '--run-dir', str(run_dir),'--config',str(config_path)]
    started = time.time()
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL)
    config = json.loads(config_path.read_text())
    os.environ.pop(config['api_key_env'], None)
    state = {'status': 'running', 'supervisor_pid': os.getpid(),
             'worker_pid': process.pid, 'started_at': started}
    while True:
        code = process.poll()
        state['heartbeat_at'] = time.time()
        if code is not None:
            state.update(status='completed' if code == 0 else 'failed',
                         exit_code=code, finished_at=time.time())
        save(run_dir/'background-status.json', state)
        if code is not None:
            print('Tournament exited with code', code, flush=True)
            return
        time.sleep(10)

if __name__ == '__main__':
    main()
