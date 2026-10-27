#!/usr/bin/env python3
"""Test exported JSON-as-YAML profiles with separate non-TUN cores; never switch the live client."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time


def curl(port, url, seconds=15, keep_trace=False):
    with tempfile.TemporaryFile() as body:
        command = ['curl', '--silent', '--show-error', '--ipv4', '--noproxy', '',
                   '--proxy', f'http://127.0.0.1:{port}', '--connect-timeout', '5',
                   '--max-time', str(seconds), '--write-out', '%{json}',
                   '--output', os.devnull, url]
        # Trace has only public diagnostic data; keep it in a pipe and never emit it.
        if keep_trace:
            command[command.index('--output') + 1] = '-'
            command[command.index('--write-out') + 1] = '\n__CURL_STATS__%{json}'
        result = subprocess.run(command, capture_output=True, text=True, timeout=seconds + 3)
        trace = ''
        try:
            if keep_trace:
                trace, raw = result.stdout.rsplit('\n__CURL_STATS__', 1)
            else:
                raw = result.stdout
            stats = json.loads(raw)
        except (ValueError, IndexError):
            stats = {}
        row = {'curl_exit': result.returncode,
               **{k: stats.get(k) for k in ['http_code', 'time_appconnect',
                                           'time_starttransfer', 'time_total',
                                           'speed_download', 'size_download', 'ssl_verify_result']}}
        return row, trace


class IndependentCore:
    def __init__(self, binary, node, parent, interface=None):
        self.binary, self.node, self.parent, self.interface = binary, node, parent, interface

    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory(prefix='probe-', dir=self.parent)
        self.root = Path(self.temp.name)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        self.port = listener.getsockname()[1]
        listener.close()
        cfg = {'mixed-port': self.port, 'bind-address': '127.0.0.1', 'allow-lan': False,
               'tun': {'enable': False}, 'dns': {'enable': False}, 'ipv6': False,
               'log-level': 'silent', 'proxies': [self.node],
               'rules': ['MATCH,' + self.node['name']]}
        if self.interface:
            cfg['interface-name'] = self.interface
        path = self.root / 'config.json'
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(cfg, out)
        self.process = subprocess.Popen([self.binary, '-d', str(self.root), '-f', str(path)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if self.process.poll() is not None:
                break
            probe = socket.socket()
            probe.settimeout(0.1)
            ready = probe.connect_ex(('127.0.0.1', self.port)) == 0
            probe.close()
            if ready:
                return self
            time.sleep(0.1)
        self.__exit__(None, None, None)
        raise RuntimeError('Independent local proxy did not start; this is not a server speed result')

    def __exit__(self, *args):
        if getattr(self, 'process', None) and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.temp.cleanup()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', type=Path, required=True,
                   help='Exported clash.yaml from this kit; its contents are valid JSON')
    p.add_argument('--core', required=True, help='Official installed Mihomo executable')
    p.add_argument('--node', action='append', help='Default: both exported nodes')
    p.add_argument('--interface', help='Optional physical interface, after checking the current route')
    p.add_argument('--rounds', type=int, default=2)
    p.add_argument('--bytes', type=int, default=8388608)
    p.add_argument('--security', action='store_true')
    p.add_argument('--out', type=Path, default=Path('probe-result.json'))
    args = p.parse_args()
    cfg = json.loads(args.profile.read_text())
    names = args.node or [n['name'] for n in cfg['proxies']]
    nodes = [n for n in cfg['proxies'] if n['name'] in names]
    if len(nodes) != len(set(names)) or any(n.get('skip-cert-verify') is not False for n in nodes):
        raise RuntimeError('Unknown node or certificate verification is not explicitly enabled')
    if not 1 <= args.rounds <= 4 or args.bytes < 1048576 or args.bytes * len(nodes) * args.rounds > 67108864:
        raise RuntimeError('Use 1..4 rounds and a total download budget of at most 64 MiB')
    parent = Path.home() / '.local/share/selfhost-vps/probes'
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    report = {'scope': 'independent non-TUN cores; does not establish whole-device protection',
              'live_client_changed': False, 'rounds': args.rounds, 'results': []}
    for node in nodes:
        row = {'node': node['name'], 'checks': []}
        with IndependentCore(args.core, node, parent, args.interface) as core:
            trace, body = curl(core.port, 'https://1.1.1.1/cdn-cgi/trace', keep_trace=True)
            trace['target'] = 'exit'
            trace['expected_exit'] = ('ip=' + node['server'] + '\n') in body
            row['checks'].append(trace)
            if trace['curl_exit'] == 0 and trace['expected_exit']:
                for number in range(args.rounds):
                    item, _ = curl(core.port, f'https://speed.cloudflare.com/__down?bytes={args.bytes}')
                    item.update(target='download', round=number + 1, requested_bytes=args.bytes)
                    item['complete'] = item['curl_exit'] == 0 and item['http_code'] == 200 and item['size_download'] == args.bytes
                    item['mbps'] = round(float(item.get('speed_download') or 0) * 8 / 1000000, 2)
                    row['checks'].append(item)
                if args.security:
                    for label, url in [('cloud_metadata', 'http://169.254.169.254/latest/meta-data/'),
                                       ('server_loopback', 'http://127.0.0.1:22/'),
                                       ('server_private_network', 'http://10.0.0.1/')]:
                        item, _ = curl(core.port, url, seconds=5)
                        item['target'] = label
                        item['unexpected_target_response'] = item.get('http_code') in [200, 201, 301, 302, 401]
                        item['note'] = 'A timeout alone does not prove firewall rejection; also inspect the server UID policy'
                        row['checks'].append(item)
        if args.security and trace['curl_exit'] == 0 and trace['expected_exit']:
            wrong = copy.deepcopy(node)
            wrong['password'] = secrets.token_urlsafe(32)
            with IndependentCore(args.core, wrong, parent, args.interface) as core:
                negative, _ = curl(core.port, 'https://1.1.1.1/cdn-cgi/trace', seconds=6, keep_trace=True)
                row['wrong_credentials_rejected'] = negative['curl_exit'] != 0
        elif args.security:
            row['wrong_credentials_rejected'] = None
            row['security_note'] = 'Positive authentication did not pass; negative test cannot establish credential isolation'
        report['results'].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error_type': type(error).__name__,
                          'detail': str(error) if isinstance(error, RuntimeError) else 'Review local prerequisites; no credentials printed'}), file=__import__('sys').stderr)
        raise SystemExit(1)
