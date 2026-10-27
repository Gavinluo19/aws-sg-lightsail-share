#!/usr/bin/env python3
"""Offline, launchd-owned rollback for TEMPORARY Mihomo config trials.

No cloud credentials, no network-setting changes, no GUI profile changes.
The original compiled profile is never overwritten. All trials roll back.
"""
import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import quote

DEFAULT_ROOT = Path.home() / '.local/share/selfhost-vps/switch-guard'
APP_HOME = Path.home() / 'Library/Application Support/io.github.clash-verge-rev.clash-verge-rev'
BINARY = '/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo'
OPEN_STATES = {'armed', 'testing', 'restoring', 'restore_failed'}
GENERAL_KEYS = ['port', 'socks-port', 'redir-port', 'tproxy-port', 'mixed-port',
                'mode', 'log-level', 'allow-lan', 'bind-address', 'ipv6', 'tun']


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    with temp.open('x', encoding='utf-8') as f:
        os.chmod(temp, 0o600)
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def yaml_read(path):
    raw = Path(path).read_text()
    try:
        return json.loads(raw)
    except ValueError:
        r = subprocess.run(['/usr/bin/ruby', '-r', 'yaml', '-r', 'json', '-e',
            'puts JSON.generate(YAML.safe_load(STDIN.read,[Symbol],[],true))'],
            input=raw, text=True, capture_output=True, timeout=10)
        if r.returncode:
            raise RuntimeError('YAML parse failed; secret-containing details withheld')
        return json.loads(r.stdout)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=8)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Guard:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.settings = read(self.root / 'settings.json', {})

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / 'state.lock').open('a') as lock:
            os.chmod(self.root / 'state.lock', 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def api(self, path, method='GET', data=None):
        connection = UnixHTTP(self.settings['socket'])
        try:
            body = json.dumps(data) if data is not None else None
            connection.request(method, path, body, {'Content-Type': 'application/json'})
            response = connection.getresponse()
            body = response.read()
            if response.status >= 300:
                raise RuntimeError('Local controller returned HTTP ' + str(response.status))
            return json.loads(body) if body else None
        finally:
            connection.close()

    def log(self, event, **fields):
        # Caller-supplied URLs, node secrets and exception messages never enter this log.
        with (self.root / 'events.jsonl').open('a') as f:
            os.chmod(self.root / 'events.jsonl', 0o600)
            f.write(json.dumps({'time': time.time(), 'event': event, **fields}) + '\n')

    def state(self):
        return read(self.root / 'state.json', {'status': 'idle'})

    def check_worker(self):
        worker = read(self.root / 'worker.json', {})
        if time.time() - worker.get('at', 0) > 8:
            raise RuntimeError('Local rollback worker is not ready; switch refused')
        os.kill(worker['pid'], 0)

    def probes(self, port):
        result = []
        for url in self.settings['probes']:
            command = ['/usr/bin/curl', '--noproxy', '', '--proxy',
                       f'http://127.0.0.1:{port}', '--silent', '--show-error',
                       '--connect-timeout', '8', '--max-time', '15',
                       '--output', '/dev/null', '--write-out', '%{http_code}', url]
            p = subprocess.run(command, capture_output=True, text=True, timeout=17)
            result.append(p.returncode == 0 and p.stdout.strip() in ('200', '204'))
        return bool(result) and all(result)

    def rules_hash(self):
        rules = self.api('/rules')['rules']
        stable = [{k: r.get(k) for k in ('type', 'payload', 'proxy')} for r in rules]
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    def snapshot(self):
        original_payload = Path(self.settings['runtime']).read_text()
        digest = hashlib.sha256(original_payload.encode()).hexdigest()
        general = self.api('/configs')
        cfg = yaml_read(self.settings['runtime'])
        # Service-mode CLI overrides may differ from the GUI-generated file.
        # Preserve the live management socket so reloads cannot strand rollback.
        cfg['external-controller-unix'] = self.settings['socket']
        proxies = self.api('/proxies')['proxies']
        own = {x['name'] for x in cfg.get('proxies', []) + cfg.get('proxy-groups', [])}
        if not own.issubset(proxies):
            raise RuntimeError('Compiled profile does not match running nodes; switch refused')
        for key in GENERAL_KEYS:
            if key in general:
                cfg[key] = general[key]
        if not self.probes(general['mixed-port']):
            raise RuntimeError('Current rollback line failed checks; switch refused')
        # Verify the file did not change during the network checks.
        if hashlib.sha256(Path(self.settings['runtime']).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Profile changed while snapshotting; switch refused')
        return {'config': cfg, 'original_payload': original_payload,
                'general': general, 'runtime_digest': digest,
                'selectors': {k: v['now'] for k, v in proxies.items()
                              if v.get('type') == 'Selector' and 'now' in v},
                'proxy_names': sorted(proxies), 'rules_hash': self.rules_hash()}

    def trial(self, candidate, seconds, enable_tun=False):
        self.check_worker()
        if self.root == DEFAULT_ROOT.resolve() and read(DEFAULT_ROOT.parent / 'ui-transaction' / 'state.json', {}).get('status') in OPEN_STATES:
            raise RuntimeError('A GUI transaction is pending; finish or restore it before a kernel trial')
        with self.lock():
            if self.state()['status'] in OPEN_STATES:
                raise RuntimeError('An unfinished trial exists; restore it first')
        snapshot = self.snapshot()
        cfg = yaml_read(candidate)
        baseline = snapshot['config']
        # A trial must not change the management plane or OS takeover settings.
        fixed = GENERAL_KEYS + ['secret', 'external-controller', 'external-controller-unix',
                               'external-controller-tls', 'external-controller-cors',
                               'external-ui', 'external-ui-url', 'authentication',
                               'listeners', 'tunnels', 'interface-name']
        for key in fixed:
            if key in baseline:
                cfg[key] = baseline[key]
            else:
                cfg.pop(key, None)
        if enable_tun:
            # Explicit whole-machine trial, still inside the same rollback transaction.
            cfg['tun'] = {**baseline.get('tun', {}), 'enable': True}
        # Keep application-owned policy selections from being overwritten by the trial.
        cfg['profile'] = {**cfg.get('profile', {}), 'store-selected': False}
        if isinstance(cfg.get('dns'), dict) and isinstance(baseline.get('dns'), dict):
            cfg['dns']['listen'] = baseline['dns'].get('listen', '')
        cfg.pop('external-ui-url', None)
        cfg['geodata-auto-update'] = False
        run = secrets.token_hex(8)
        trial_file = self.root / ('validate-' + run + '.json')
        # Validation is local; stdout/stderr can contain secrets, so never display it.
        try:
            for description, content in [('Rollback', snapshot['config']), ('Candidate', cfg)]:
                atomic(trial_file, content)
                p = subprocess.run([self.settings['binary'], '-t', '-d', self.settings['home'],
                                    '-f', str(trial_file)], capture_output=True, timeout=20)
                if p.returncode:
                    raise RuntimeError(description + ' validation failed; no switch performed')
        finally:
            trial_file.unlink(missing_ok=True)
        with self.lock():
            if self.state()['status'] in OPEN_STATES:
                raise RuntimeError('Another trial was armed; switch refused')
            if hashlib.sha256(Path(self.settings['runtime']).read_bytes()).hexdigest() != snapshot['runtime_digest']:
                raise RuntimeError('Profile changed during preflight; switch refused')
            self.check_worker()
            atomic(self.root / 'snapshot.json', snapshot)
            state = {'status': 'armed', 'id': run, 'armed_at': time.time(),
                     'deadline': time.time() + seconds,
                     'deadline_monotonic': time.monotonic() + seconds, 'seconds': seconds}
            atomic(self.root / 'state.json', state)
            self.log('armed', id=run, seconds=seconds)
        # Do not touch the running configuration before launchd's worker ACKs this trial.
        for _ in range(40):
            worker = read(self.root / 'worker.json', {})
            if worker.get('ack') == run:
                break
            time.sleep(0.1)
        else:
            self.restore('worker_ack_missing')
            raise RuntimeError('Rollback worker did not acknowledge; switch refused')
        with self.lock():
            state = self.state()
            if state.get('id') != run or state['status'] != 'armed':
                raise RuntimeError('Trial already restored; switch refused')
            # Once armed, errors or process death never disarm rollback.
            self.api('/configs?force=true', 'PUT', {'payload': json.dumps(cfg, ensure_ascii=False)})
            state['status'] = 'testing'
            atomic(self.root / 'state.json', state)
            self.log('candidate_applied', id=run)
        return {'status': 'testing', 'id': run, 'automatic_restore_seconds': seconds}

    def restore(self, reason='manual'):
        with self.lock():
            state = self.state()
            if state['status'] not in OPEN_STATES:
                return {'status': state['status'], 'action': 'none'}
            snapshot = read(self.root / 'snapshot.json')
            state.update(status='restoring', restore_reason=reason)
            atomic(self.root / 'state.json', state)
            try:
                try:
                    self.api('/configs?force=true', 'PUT', {'payload': json.dumps(snapshot['config'], ensure_ascii=False)})
                except Exception:
                    # A separate lossless fallback for serialization/reload errors.
                    self.api('/configs?force=true', 'PUT', {'payload': snapshot['original_payload']})
                    self.api('/configs', 'PATCH',
                             {k: snapshot['general'][k] for k in GENERAL_KEYS if k in snapshot['general']})
                    self.log('original_payload_fallback', id=state['id'])
                for group, selected in snapshot['selectors'].items():
                    self.api('/proxies/' + quote(group, safe=''), 'PUT', {'name': selected})
                current = self.api('/configs')
                nodes = self.api('/proxies')['proxies']
                assert sorted(nodes) == snapshot['proxy_names']
                assert self.rules_hash() == snapshot['rules_hash']
                for key in ('mode', 'mixed-port', 'allow-lan', 'ipv6'):
                    assert current.get(key) == snapshot['general'].get(key)
                assert current.get('tun', {}).get('enable') == snapshot['general'].get('tun', {}).get('enable')
                for group, selected in snapshot['selectors'].items():
                    assert nodes[group]['now'] == selected
                state.update(status='restored', restored_at=time.time(),
                             elapsed=round(time.time() - state['armed_at'], 2),
                             restoration_verified=True)
                state.pop('error', None)
                state.pop('retry_at', None)
                self.log('restored', id=state['id'], reason=reason, elapsed=state['elapsed'])
                # Expire temporary credential copies after successful restoration.
                for filename in ('candidate.json', 'snapshot.json'):
                    (self.root / filename).unlink(missing_ok=True)
            except Exception as e:
                state.update(status='restore_failed', retry_at=time.time()+5, error=type(e).__name__)
                self.log('restore_failed', id=state['id'], error=type(e).__name__)
            atomic(self.root / 'state.json', state)
            return {k: v for k, v in state.items() if k not in ('error',)}

    def worker(self):
        # launchd owns this process. Parent exit, Codex loss and failed Internet probes
        # do not change the deadline. On wake/login an overdue trial restores at once.
        worker_lock = (self.root / 'worker.lock').open('a')
        fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            state = self.state()
            atomic(self.root / 'worker.json', {'pid': os.getpid(), 'at': time.time(),
                                              'ack': state.get('id')})
            due = (time.time() >= state.get('deadline', float('inf')) or
                   time.monotonic() >= state.get('deadline_monotonic', float('inf')))
            if state['status'] in OPEN_STATES and due and time.time() >= state.get('retry_at', 0):
                self.restore('deadline')
            time.sleep(1)


def install(args, guard):
    root = guard.root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    if guard.state()['status'] in OPEN_STATES:
        raise RuntimeError('Restore the pending trial before installing')
    settings = {'socket': args.socket, 'runtime': str(Path(args.runtime).resolve()),
                'home': str(Path(args.home).resolve()), 'binary': BINARY,
                'probes': args.probe or ['https://www.google.com/generate_204', 'https://checkip.amazonaws.com'],
                'label': args.label}
    atomic(root / 'settings.json', settings)
    script = root / 'switch_guard.py'
    script.write_bytes(Path(__file__).read_bytes())
    script.chmod(0o700)
    agent = Path.home() / 'Library/LaunchAgents' / (args.label+'.plist')
    agent.parent.mkdir(exist_ok=True)
    log = root / 'worker.log'
    log.touch(mode=0o600)
    spec = {'Label': args.label,
            'ProgramArguments': [sys.executable, str(script), '--root', str(root), 'worker'],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 2,
            'ProcessType': 'Background', 'Umask': 0o077,
            'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
    agent.write_bytes(plistlib.dumps(spec)); agent.chmod(0o600)
    domain = f'gui/{os.getuid()}'
    subprocess.run(['/bin/launchctl', 'bootout', domain+'/'+args.label], capture_output=True)
    for attempt in range(20):
        p = subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(agent)], capture_output=True)
        if p.returncode == 0:break
        time.sleep(.2)
    else:raise RuntimeError('Could not start rollback worker')
    for _ in range(50):
        try:
            guard.check_worker()
            return {'installed': True, 'label': args.label, 'local_only': True}
        except Exception:
            time.sleep(.1)
    raise RuntimeError('launchd worker did not start')


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    sub = p.add_subparsers(dest='command', required=True)
    i = sub.add_parser('install')
    live_service_socket = f'/var/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock'
    i.add_argument('--socket', default=live_service_socket if Path(live_service_socket).exists() else '/tmp/verge/verge-mihomo.sock')
    i.add_argument('--runtime', default=str(APP_HOME/'clash-verge.yaml'))
    i.add_argument('--home', default=str(APP_HOME))
    i.add_argument('--label', default='local.selfhost.vps.switch-guard')
    i.add_argument('--probe', action='append')
    t = sub.add_parser('trial')
    t.add_argument('candidate', type=Path)
    t.add_argument('--seconds', type=int, default=120)
    t.add_argument('--enable-tun', action='store_true',
                   help='Temporarily enable TUN after the watchdog ACK; restore original state')
    sub.add_parser('restore')
    sub.add_parser('status')
    sub.add_parser('worker')
    args = p.parse_args()
    guard = Guard(args.root)
    try:
        if args.command == 'install':
            result = install(args, guard)
        elif args.command == 'trial':
            if not 8 <= args.seconds <= 300:
                raise RuntimeError('Deadline must be between 8 and 300 seconds')
            result = guard.trial(args.candidate, args.seconds, args.enable_tun)
        elif args.command == 'restore':
            result = guard.restore()
        elif args.command == 'worker':
            guard.worker(); return
        else:
            result = guard.state()
            try:
                guard.check_worker(); result['worker_ready'] = True
            except Exception:
                result['worker_ready'] = False
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        # No raw traceback or failed subprocess output: configs contain private keys.
        print(json.dumps({'ok': False, 'error_type': type(e).__name__,
                          'message': str(e) if isinstance(e, RuntimeError) else 'Operation failed; check local guard status'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
