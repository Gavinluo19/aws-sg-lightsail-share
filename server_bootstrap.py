#!/usr/bin/env python3
"""Fresh Ubuntu 24.04 server only. Plan by default; no credentials on stdout."""
import argparse
import copy
import datetime as dt
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

BUILD = '2026-10-25-public-v1'
SING_BOX = '1.14.4'
CERTBOT = '5.8.0'
ASSETS = {
    'x86_64': ('linux-amd64', '699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968'),
    'aarch64': ('linux-arm64', '436441f76ee2f7fe21b5b2d426d3cb261a480455a704865789ad533e4273ec26'),
}
ETC = Path('/etc/selfhost-vps')
VAR = Path('/var/lib/selfhost-vps')
OPT = Path('/opt/selfhost-vps')
PROFILES = Path('/srv/selfhost-vps/profiles')
STATE = VAR / 'state.json'
PENDING = VAR / 'pending-state.json'
BINARY = '/usr/local/bin/selfhost-sing-box'
SERVICE_USER = 'selfhost-vps'
PRIVATE_V4 = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
              '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.168.0.0/16',
              '198.18.0.0/15', '224.0.0.0/3']
PRIVATE_V6 = ['::/128', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8']


def atomic(path, content, mode=0o600, group=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if group is not None:
            os.fchown(fd, 0, group)
        with os.fdopen(fd, 'wb') as out:
            out.write(content if isinstance(content, bytes) else content.encode())
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def run(args, *, data=None, timeout=180):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    # No raw stderr: configuration errors can contain passwords or private paths.
    if result.returncode:
        if os.geteuid() == 0:
            VAR.mkdir(parents=True, exist_ok=True)
            fd = os.open(VAR / 'private-command-errors.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'w') as log:
                log.write(json.dumps({'time': dt.datetime.now(dt.timezone.utc).isoformat(),
                                      'tool': Path(args[0]).name, 'exit': result.returncode,
                                      'stdout_tail': result.stdout[-4000:].decode(errors='replace'),
                                      'stderr_tail': result.stderr[-4000:].decode(errors='replace')}) + '\n')
        raise RuntimeError('Command failed: ' + Path(args[0]).name)
    return result.stdout


def valid_name(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{1,31}', value):
        raise ValueError('Use a device label such as laptop-01 or phone-01')
    return value


def new_device():
    return {'status': 'active', 'token': secrets.token_urlsafe(32),
            'hy2_password': secrets.token_urlsafe(32),
            'trojan_password': secrets.token_urlsafe(32)}


def initial_state(ip, email, names, download_mbps=0, direct_domains=None):
    return {'build': BUILD, 'public_ip': ip, 'email': email,
            'download_mbps': download_mbps, 'direct_domains': direct_domains or [],
            'ws_path': '/t/' + secrets.token_urlsafe(24),
            'devices': {valid_name(n): new_device() for n in names}}


def server_config(state, cert_path=None, key_path=None):
    active = {n: d for n, d in state['devices'].items() if d['status'] == 'active'}
    cert_path = cert_path or str(ETC / 'tls/fullchain.pem')
    key_path = key_path or str(ETC / 'tls/privkey.pem')
    return {
        'log': {'level': 'warn', 'timestamp': True},
        'dns': {'servers': [{'type': 'https', 'tag': 'secure-dns',
                            'server': '1.1.1.1', 'server_port': 443,
                            'path': '/dns-query',
                            'tls': {'enabled': True, 'server_name': 'cloudflare-dns.com'}}],
                'final': 'secure-dns', 'strategy': 'ipv4_only'},
        'inbounds': [
            {'type': 'hysteria2', 'tag': 'fast-in', 'listen': '0.0.0.0',
             'listen_port': 51821,
             'users': [{'name': n, 'password': d['hy2_password']} for n, d in active.items()],
             'tls': {'enabled': True, 'min_version': '1.3',
                     'certificate_path': cert_path, 'key_path': key_path}},
            {'type': 'trojan', 'tag': 'tcp-in', 'listen': '127.0.0.1',
             'listen_port': 10000,
             'users': [{'name': n, 'password': d['trojan_password']} for n, d in active.items()],
             'transport': {'type': 'ws', 'path': state['ws_path']}},
        ],
        'outbounds': [{'type': 'direct', 'tag': 'direct'}],
        'route': {'rules': [{'ip_cidr': PRIVATE_V4 + PRIVATE_V6 + [state['public_ip'] + '/32'],
                             'action': 'reject'}],
                  'default_domain_resolver': {'server': 'secure-dns', 'strategy': 'ipv4_only'},
                  'final': 'direct'},
    }


def clash_profile(state, name):
    d = state['devices'][name]
    fast = {'name': 'AWS-FAST', 'type': 'hysteria2', 'server': state['public_ip'],
            'port': 51821, 'password': d['hy2_password'], 'sni': state['public_ip'],
            'skip-cert-verify': False, 'alpn': ['h3']}
    if state['download_mbps']:
        fast['down'] = str(state['download_mbps']) + ' Mbps'
    tcp = {'name': 'AWS-TCP', 'type': 'trojan', 'server': state['public_ip'],
           'port': 443, 'password': d['trojan_password'], 'sni': state['public_ip'],
           'skip-cert-verify': False, 'network': 'ws',
           'ws-opts': {'path': state['ws_path']}, 'udp': False}
    protected = ['anthropic.com', 'claude.ai', 'claude.com', 'claudeusercontent.com',
                 'openai.com', 'chatgpt.com', 'challenges.cloudflare.com',
                 'ippure.com', 'browserleaks.com']
    rules = [f'IP-CIDR,{state["public_ip"]}/32,DIRECT,no-resolve']
    rules += [f'DOMAIN-SUFFIX,{domain},PROXY' for domain in protected]
    rules += [f'IP-CIDR,{cidr},DIRECT,no-resolve' for cidr in PRIVATE_V4]
    rules += [f'DOMAIN-SUFFIX,{domain},DIRECT' for domain in state['direct_domains']]
    rules += ['MATCH,PROXY']
    dns_policy = {f'+.{domain}': ['system'] for domain in ['local', 'lan', 'home.arpa']}
    dns_policy.update({f'+.{domain}': ['https://dns.alidns.com/dns-query#DIRECT']
                       for domain in state['direct_domains']})
    return {'mixed-port': 7890, 'bind-address': '127.0.0.1', 'allow-lan': False,
            'mode': 'rule', 'ipv6': False, 'log-level': 'warning',
            'profile': {'store-selected': True}, 'tun': {'enable': False},
            'dns': {'enable': True, 'listen': '127.0.0.1:1053', 'ipv6': False,
                    'enhanced-mode': 'fake-ip', 'respect-rules': True,
                    'default-nameserver': ['1.1.1.1'],
                    'nameserver': ['https://1.1.1.1/dns-query#AWS-TCP',
                                   'https://8.8.8.8/dns-query#PROXY'],
                    'proxy-server-nameserver': ['1.1.1.1'],
                    'nameserver-policy': dns_policy,
                    'fake-ip-filter': ['*.lan', '*.local', 'localhost', '*.home.arpa']},
            'proxies': [fast, tcp],
            'proxy-groups': [{'name': 'PROXY', 'type': 'select',
                              'proxies': ['AWS-FAST', 'AWS-TCP'],
                              'default-selected': 'AWS-FAST'}],
            'rules': rules}


def shadowrocket_profile(state, name):
    d = state['devices'][name]
    ip = state['public_ip']
    lines = ['# Self-hosted private profile. Never post this file publicly.',
             '[General]', 'bypass-system = true',
             'skip-proxy = localhost, *.local, 10.0.0.0/8, 127.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16',
             'dns-server = https://1.1.1.1/dns-query,https://8.8.8.8/dns-query',
             'dns-direct-system = true', 'dns-direct-fallback-proxy = false',
             'ipv6 = false', 'prefer-ipv6 = false',
             'udp-policy-not-supported-behaviour = REJECT',
             'update-url = https://' + ip + '/p/' + d['token'] + '/shadowrocket.conf',
             '', '[Proxy]',
             f'AWS-FAST = hysteria2,{ip},51821,password={d["hy2_password"]},sni={ip},udp=1',
             f'AWS-TCP = trojan,{ip},443,password={d["trojan_password"]},sni={ip},ws=1,ws-path={state["ws_path"]},udp=0',
             '', '[Proxy Group]', 'PROXY = select,AWS-FAST,AWS-TCP', '', '[Rule]',
             f'IP-CIDR,{ip}/32,DIRECT,no-resolve']
    for domain in ['anthropic.com', 'claude.ai', 'claude.com', 'claudeusercontent.com',
                   'openai.com', 'chatgpt.com', 'ippure.com', 'browserleaks.com']:
        lines += [f'DOMAIN-SUFFIX,{domain},PROXY']
    lines += [f'IP-CIDR,{cidr},DIRECT,no-resolve' for cidr in PRIVATE_V4]
    lines += [f'DOMAIN-SUFFIX,{domain},DIRECT' for domain in state['direct_domains']]
    return '\n'.join(lines + ['FINAL,PROXY', ''])


def nginx_site(state, tls=True):
    ip = state['public_ip']
    text = f'''server {{
    listen 80 default_server;
    server_name {ip};
    server_tokens off;
    access_log off;
    error_log /var/log/nginx/selfhost-vps-error.log crit;
    location ^~ /.well-known/acme-challenge/ {{
        root /var/www/selfhost-acme;
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 404; }}
}}
'''
    if not tls:
        return text
    text += f'''server {{
    listen 443 ssl default_server;
    server_name {ip};
    server_tokens off;
    access_log off;
    error_log /var/log/nginx/selfhost-vps-error.log crit;
    ssl_certificate /etc/letsencrypt/live/selfhost-ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/selfhost-ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    location = /health {{
        alias /var/lib/selfhost-vps/public-health.json;
        default_type application/json;
        add_header Cache-Control no-store always;
    }}
    location = {state['ws_path']} {{
        limit_except GET {{ deny all; }}
        proxy_pass http://127.0.0.1:10000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
        proxy_buffering off;
        proxy_request_buffering off;
    }}
'''
    for name, d in state['devices'].items():
        if d['status'] != 'active':
            continue
        for file in ['clash.yaml', 'shadowrocket.conf', 'node.yaml']:
            text += f'''    location = /p/{d['token']}/{file} {{
        alias /srv/selfhost-vps/profiles/{name}/{file};
        default_type text/plain;
        add_header Cache-Control no-store always;
        log_not_found off;
        limit_except GET {{ deny all; }}
    }}
'''
    return text + '    location / { return 404; }\n}\n'


def firewall_source(state, uid):
    private = ', '.join(PRIVATE_V4 + [state['public_ip'] + '/32'])
    return f'''table inet selfhost_vps {{
 chain input {{
  type filter hook input priority 0; policy drop;
  iifname "lo" accept
  ct state established,related accept
  tcp dport {{ 22, 80, 443 }} accept
  udp dport 51821 accept
  udp sport 67 udp dport 68 accept
  meta l4proto {{ icmp, ipv6-icmp }} accept
 }}
 chain output {{
  type filter hook output priority 0; policy accept;
  ct state established,related accept
  meta skuid {uid} ip daddr {{ {private} }} reject
  meta skuid {uid} meta nfproto ipv6 reject
 }}
 chain forward {{
  type filter hook forward priority 0; policy drop;
 }}
}}
'''


FIREWALL_HELPER = '''#!/usr/bin/env python3
import pathlib,subprocess,tempfile,os
p=pathlib.Path('/etc/selfhost-vps/firewall.nft')
exists=subprocess.run(['nft','list','table','inet','selfhost_vps'],capture_output=True).returncode==0
text=('delete table inet selfhost_vps\\n' if exists else '')+p.read_text()
fd,name=tempfile.mkstemp(prefix='selfhost-fw-',dir='/run')
try:
 os.fchmod(fd,0o600)
 with os.fdopen(fd,'w') as f:f.write(text)
 subprocess.run(['nft','-c','-f',name],check=True,capture_output=True)
 subprocess.run(['nft','-f',name],check=True,capture_output=True)
finally:pathlib.Path(name).unlink(missing_ok=True)
'''

HEALTH_HELPER = '''#!/usr/bin/env python3
import datetime,json,pathlib,subprocess,sys
def good(args):return subprocess.run(args,capture_output=True).returncode==0
checks={u:good(['systemctl','is-active','--quiet',u]) for u in ['nginx','selfhost-proxy','selfhost-firewall']}
checks['certificate_over_48h']=good(['openssl','x509','-in','/etc/selfhost-vps/tls/fullchain.pem','-checkend','172800','-noout'])
d={'status':'ok' if all(checks.values()) else 'degraded','build':'2026-10-25-public-v1','checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':checks}
p=pathlib.Path('/var/lib/selfhost-vps/public-health.json');p.write_text(json.dumps(d));p.chmod(0o644)
sys.exit(0 if d['status']=='ok' else 1)
'''

CERT_HOOK = '''#!/usr/bin/env bash
set -euo pipefail
install -o root -g selfhost-vps -m 0640 /etc/letsencrypt/live/selfhost-ip/fullchain.pem /etc/selfhost-vps/tls/fullchain.pem
install -o root -g selfhost-vps -m 0640 /etc/letsencrypt/live/selfhost-ip/privkey.pem /etc/selfhost-vps/tls/privkey.pem
/usr/local/bin/selfhost-sing-box check -c /etc/selfhost-vps/config.json >/dev/null 2>&1
/usr/sbin/nginx -t >/dev/null 2>&1
systemctl reload nginx
systemctl restart selfhost-proxy
/usr/bin/python3 /opt/selfhost-vps/health.py
'''


def install_packages():
    run(['apt-get', 'update'], timeout=300)
    run(['apt-get', 'install', '-y', 'nginx', 'nftables', 'curl', 'ca-certificates',
         'openssl', 'python3-venv', 'unattended-upgrades'], timeout=600)
    if not shutil.which('sshd') and not Path('/usr/sbin/sshd').exists():
        raise RuntimeError('OpenSSH is required for the browser recovery path')
    if not Path('/opt/selfhost-certbot/bin/certbot').exists():
        run(['python3', '-m', 'venv', '/opt/selfhost-certbot'], timeout=120)
        run(['/opt/selfhost-certbot/bin/pip', 'install', '--disable-pip-version-check',
             'certbot==' + CERTBOT], timeout=600)
    arch, digest = ASSETS[platform.machine()]
    url = f'https://github.com/SagerNet/sing-box/releases/download/v{SING_BOX}/sing-box-{SING_BOX}-{arch}.tar.gz'
    with tempfile.TemporaryDirectory(prefix='selfhost-download-', dir='/var/tmp') as tmp:
        archive = Path(tmp) / 'sing-box.tar.gz'
        with urllib.request.urlopen(url, timeout=45) as src, archive.open('wb') as dst:
            shutil.copyfileobj(src, dst)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise RuntimeError('Official release SHA256 did not match')
        with tarfile.open(archive) as tar:
            candidates = [m for m in tar.getmembers() if m.isfile() and m.name.endswith('/sing-box')]
            if len(candidates) != 1:
                raise RuntimeError('Unexpected release archive')
            atomic(BINARY, tar.extractfile(candidates[0]).read(), 0o755)


def check_host(args, existing):
    if os.geteuid() != 0:
        raise RuntimeError('--apply requires sudo on the NEW server')
    os_release = dict(l.split('=', 1) for l in Path('/etc/os-release').read_text().splitlines() if '=' in l)
    if os_release.get('ID', '').strip('"') != 'ubuntu' or os_release.get('VERSION_ID', '').strip('"') != '24.04':
        raise RuntimeError('This baseline supports fresh Ubuntu 24.04 only')
    if platform.machine() not in ASSETS:
        raise RuntimeError('Unsupported CPU architecture')
    if not ipaddress.ip_address(args.public_ip).is_global:
        raise RuntimeError('A real public IPv4 address is required')
    if not existing:
        if not args.confirm_fresh_server:
            raise RuntimeError('Confirm that this is a fresh dedicated server')
        if not args.accept_acme_terms:
            raise RuntimeError('The owner must review and accept the ACME subscriber terms')
        for path in ['/etc/selfhost-vps', '/var/lib/selfhost-vps',
                     '/srv/selfhost-vps', '/etc/nginx/sites-enabled/selfhost-vps']:
            if Path(path).exists():
                raise RuntimeError('Unexpected existing deployment; refusing overwrite')
        if any(x.pw_name == SERVICE_USER for x in pwd.getpwall()):
            raise RuntimeError('A pre-existing service identity needs review')
        sites = Path('/etc/nginx/sites-enabled')
        if sites.exists() and any(p.name != 'default' for p in sites.iterdir()):
            raise RuntimeError('Existing websites found; use a fresh dedicated server')
        for kind, port in [(socket.SOCK_STREAM, 80), (socket.SOCK_STREAM, 443),
                           (socket.SOCK_STREAM, 10000), (socket.SOCK_DGRAM, 51821)]:
            with socket.socket(socket.AF_INET, kind) as probe:
                try:
                    probe.bind(('0.0.0.0', port))
                except OSError:
                    raise RuntimeError('A required port is already used; use a fresh dedicated server')
        if shutil.which('nft'):
            raw = run(['nft', '-j', 'list', 'ruleset'])
            if any('table' in row for row in json.loads(raw).get('nftables', [])):
                raise RuntimeError('Existing firewall tables found; review them instead of overwriting')
    elif not args.repair:
        raise RuntimeError('Existing installation found; use --repair to preserve its identities')


def issue_certificate(state):
    common = ['/opt/selfhost-certbot/bin/certbot', 'certonly', '--non-interactive',
              '--agree-tos', '--email', state['email'],
              '--preferred-profile', 'shortlived', '--webroot',
              '--webroot-path', '/var/www/selfhost-acme', '--ip-address', state['public_ip']]
    # The staging certificate is never installed in either public service.
    run(common + ['--staging', '--cert-name', 'selfhost-ip-test'], timeout=300)
    run(common + ['--cert-name', 'selfhost-ip'], timeout=300)


def enable_units():
    units = {
        'selfhost-firewall.service': '''[Unit]
Description=Owned self-hosted VPS firewall
Before=selfhost-proxy.service nginx.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 /opt/selfhost-vps/apply_firewall.py
ExecReload=/usr/bin/python3 /opt/selfhost-vps/apply_firewall.py
[Install]
WantedBy=multi-user.target
''',
        'selfhost-proxy.service': '''[Unit]
Description=Authenticated self-hosted proxy
After=network-online.target selfhost-firewall.service
Requires=selfhost-firewall.service
Wants=network-online.target
[Service]
User=selfhost-vps
Group=selfhost-vps
ExecStart=/usr/local/bin/selfhost-sing-box run -c /etc/selfhost-vps/config.json
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
LimitNOFILE=16384
MemoryMax=512M
UMask=0077
[Install]
WantedBy=multi-user.target
''',
        'selfhost-renew.service': '''[Unit]
Description=Renew the short-lived IP certificate
After=network-online.target nginx.service
[Service]
Type=oneshot
ExecStart=/opt/selfhost-certbot/bin/certbot renew --cert-name selfhost-ip --quiet --no-random-sleep-on-renew --deploy-hook /opt/selfhost-vps/reload-certificates.sh
TimeoutStartSec=300
''',
        'selfhost-renew.timer': '''[Unit]
Description=Check renewal every six hours
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=1800
Persistent=yes
[Install]
WantedBy=timers.target
''',
        'selfhost-health.service': '''[Unit]
Description=Check proxy services and certificate lifetime
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/selfhost-vps/health.py
TimeoutStartSec=30
''',
        'selfhost-health.timer': '''[Unit]
Description=Check local health every 30 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=30min
Persistent=yes
[Install]
WantedBy=timers.target
''',
    }
    for name, body in units.items():
        atomic(Path('/etc/systemd/system') / name, body, 0o644)
    run(['systemctl', 'daemon-reload'])
    # The script owns a separate table/service and never flushes the host ruleset.
    run(['systemctl', 'disable', 'nftables'])
    run(['systemctl', 'enable', 'selfhost-firewall', 'selfhost-proxy',
         'selfhost-renew.timer', 'selfhost-health.timer'])


def configure(state):
    uid = pwd.getpwnam(SERVICE_USER).pw_uid
    gid = grp.getgrnam(SERVICE_USER).gr_gid
    www = grp.getgrnam('www-data').gr_gid
    for folder, mode, group in [(ETC, 0o750, gid), (ETC / 'tls', 0o750, gid),
                                (VAR, 0o755, 0), (OPT, 0o755, 0), (PROFILES, 0o750, www)]:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, mode)
        os.chown(folder, 0, group)
    source = Path('/etc/letsencrypt/live/selfhost-ip')
    for file in ['fullchain.pem', 'privkey.pem']:
        atomic(ETC / 'tls' / file, (source / file).read_bytes(), 0o640, gid)
    atomic(ETC / 'config.json', json.dumps(server_config(state), indent=2), 0o640, gid)
    run([BINARY, 'check', '-c', str(ETC / 'config.json')])
    atomic(ETC / 'firewall.nft', firewall_source(state, uid), 0o600)
    atomic(OPT / 'apply_firewall.py', FIREWALL_HELPER, 0o700)
    atomic(OPT / 'health.py', HEALTH_HELPER, 0o700)
    atomic(OPT / 'reload-certificates.sh', CERT_HOOK, 0o700)
    handoff = ['# Private device subscriptions', '', 'Treat every URL as a password.', '']
    for name, d in state['devices'].items():
        if d['status'] != 'active':
            continue
        folder = PROFILES / name
        folder.mkdir(exist_ok=True)
        os.chmod(folder, 0o750)
        os.chown(folder, 0, www)
        clash = clash_profile(state, name)
        files = {'clash.yaml': json.dumps(clash, ensure_ascii=False, indent=2),
                 'node.yaml': json.dumps({'proxies': clash['proxies']}, indent=2),
                 'shadowrocket.conf': shadowrocket_profile(state, name)}
        for file, body in files.items():
            atomic(folder / file, body, 0o640, www)
        handoff += [name, f'https://{state["public_ip"]}/p/{d["token"]}/clash.yaml',
                    f'https://{state["public_ip"]}/p/{d["token"]}/shadowrocket.conf', '']
    atomic(VAR / 'handoff.md', '\n'.join(handoff), 0o600)
    atomic('/etc/nginx/sites-available/selfhost-vps', nginx_site(state), 0o600)
    run(['/usr/sbin/nginx', '-t'])
    enable_units()
    run(['systemctl', 'restart', 'selfhost-firewall'])
    run(['systemctl', 'reload', 'nginx'])
    run(['systemctl', 'restart', 'selfhost-proxy'])
    run(['systemctl', 'start', 'selfhost-renew.timer', 'selfhost-health.timer'])
    run(['python3', str(OPT / 'health.py')])
    atomic(STATE, json.dumps(state, indent=2), 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-ip')
    parser.add_argument('--email')
    parser.add_argument('--devices', default='laptop-01,phone-01')
    parser.add_argument('--direct-domains', default=None)
    parser.add_argument('--download-mbps', type=int, default=None,
                        help='0 means adaptive; choose a fixed number only after path measurements')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-fresh-server', action='store_true')
    parser.add_argument('--accept-acme-terms', action='store_true')
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--revoke')
    parser.add_argument('--add-device')
    args = parser.parse_args()
    complete = STATE.exists()
    existing = complete or PENDING.exists()
    old_state = json.loads((STATE if complete else PENDING).read_text()) if existing else None
    args.public_ip = args.public_ip or (old_state or {}).get('public_ip')
    args.email = args.email or (old_state or {}).get('email')
    if not args.public_ip or not args.email:
        raise RuntimeError('Provide --public-ip and --email; never provide AWS keys or AI passwords')
    ipaddress.IPv4Address(args.public_ip)
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
        raise RuntimeError('Invalid contact email')
    rate = args.download_mbps if args.download_mbps is not None else (old_state or {}).get('download_mbps', 0)
    if not 0 <= rate <= 1000:
        raise RuntimeError('Download rate must be 0..1000 Mbps')
    names = [valid_name(x.strip()) for x in args.devices.split(',') if x.strip()]
    if not 1 <= len(set(names)) <= 6:
        raise RuntimeError('Use 1..6 unique device labels')
    state = copy.deepcopy(old_state) if old_state else initial_state(args.public_ip, args.email, names)
    if state['public_ip'] != args.public_ip:
        raise RuntimeError('Static IP changed; review certificates and device delivery separately')
    state['download_mbps'] = rate
    if args.direct_domains is not None or not old_state:
        domains = [d.strip().lower() for d in (args.direct_domains or '').split(',') if d.strip()]
        if any(not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', d) for d in domains):
            raise RuntimeError('Invalid direct domain')
        protected = ['anthropic.com', 'claude.ai', 'claude.com', 'claudeusercontent.com',
                     'openai.com', 'chatgpt.com', 'ippure.com', 'browserleaks.com']
        if any(d == p or p.endswith('.' + d) or d.endswith('.' + p) for d in domains for p in protected):
            raise RuntimeError('A protected AI or diagnostic domain cannot be added to DIRECT')
        state['direct_domains'] = domains
    if args.revoke:
        valid_name(args.revoke)
        if args.revoke not in state['devices']:
            raise RuntimeError('Unknown device')
        state['devices'][args.revoke]['status'] = 'revoked'
    if args.add_device:
        valid_name(args.add_device)
        if args.add_device in state['devices']:
            raise RuntimeError('Use a new label; an old device identity is never silently reused')
        state['devices'][args.add_device] = new_device()
    active = sum(d['status'] == 'active' for d in state['devices'].values())
    if not 1 <= active <= 6:
        raise RuntimeError('Keep 1..6 active devices; deliberate service shutdown is separate')
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'build': BUILD, 'sing_box': SING_BOX,
                          'certbot': CERTBOT, 'active_devices': active,
                          'download_mbps': rate or 'adaptive',
                          'ports': {'public_tcp': [80, 443], 'public_udp': [51821],
                                    'ssh': 'AWS browser SSH alias only', 'private_tcp': [10000]},
                          'credentials_generated_on_server': True}, ensure_ascii=False))
        return
    check_host(args, existing)
    os.umask(0o077)
    VAR.mkdir(parents=True, exist_ok=True)
    snapshot = VAR / 'backups' / dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    snapshot.mkdir(parents=True, mode=0o700)
    os.chmod(snapshot.parent, 0o700)
    tracked = [ETC, PROFILES, STATE, OPT, Path('/etc/nginx/sites-available/selfhost-vps')]
    tracked += [Path('/etc/systemd/system') / name for name in
                ['selfhost-firewall.service', 'selfhost-proxy.service', 'selfhost-renew.service',
                 'selfhost-renew.timer', 'selfhost-health.service', 'selfhost-health.timer']]
    saved = []
    for i, p in enumerate(tracked):
        if p.exists():
            dst = snapshot / str(i)
            shutil.copytree(p, dst) if p.is_dir() else shutil.copy2(p, dst)
            saved.append((p, dst))
    try:
        if not complete:
            if not (args.accept_acme_terms or state.get('acme_terms_accepted')):
                raise RuntimeError('The owner must accept the ACME subscriber terms before issuance')
            state['acme_terms_accepted'] = True
            atomic(PENDING, json.dumps(state, indent=2), 0o600)
            install_packages()
            if not any(x.pw_name == SERVICE_USER for x in pwd.getpwall()):
                run(['useradd', '--system', '--user-group', '--no-create-home',
                     '--shell', '/usr/sbin/nologin', SERVICE_USER])
            Path('/var/www/selfhost-acme/.well-known/acme-challenge').mkdir(parents=True, exist_ok=True)
            default = Path('/etc/nginx/sites-enabled/default')
            if default.is_symlink():
                default.unlink()
            atomic('/etc/nginx/sites-available/selfhost-vps', nginx_site(state, tls=False), 0o600)
            enabled = Path('/etc/nginx/sites-enabled/selfhost-vps')
            if enabled.is_symlink():
                if str(enabled.readlink()) != '/etc/nginx/sites-available/selfhost-vps':
                    raise RuntimeError('Unexpected webserver link; refusing overwrite')
            elif enabled.exists():
                raise RuntimeError('Unexpected webserver site file; refusing overwrite')
            else:
                enabled.symlink_to('/etc/nginx/sites-available/selfhost-vps')
            run(['/usr/sbin/nginx', '-t'])
            run(['systemctl', 'enable', '--now', 'nginx'])
            run(['systemctl', 'reload', 'nginx'])
            issue_certificate(state)
            # Preserve the browser recovery mechanism; disable password/root login only.
            atomic('/etc/ssh/sshd_config.d/50-selfhost-vps.conf',
                   'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n', 0o644)
            run(['/usr/sbin/sshd', '-t'])
            run(['systemctl', 'reload', 'ssh'])
            OPT.mkdir(parents=True, exist_ok=True)
            atomic(OPT / 'server_bootstrap.py', Path(__file__).read_bytes(), 0o700)
        configure(state)
        PENDING.unlink(missing_ok=True)
        print(json.dumps({'status': 'configured', 'build': BUILD, 'active_devices': active,
                          'handoff_file': '/var/lib/selfhost-vps/handoff.md',
                          'credentials_on_stdout': False,
                          'fresh_cloud_end_to_end_acceptance': 'required'}, ensure_ascii=False))
    except Exception:
        for p, backup in saved:
            if backup.is_dir():
                if p.exists():
                    shutil.rmtree(p)
                shutil.copytree(backup, p)
            else:
                shutil.copy2(backup, p)
        if complete:
            subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
            for unit in ['selfhost-firewall', 'nginx', 'selfhost-proxy']:
                subprocess.run(['systemctl', 'restart', unit], capture_output=True)
        else:
            subprocess.run(['systemctl', 'stop', 'selfhost-proxy'], capture_output=True)
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error_type': type(error).__name__,
                          'detail': str(error) if isinstance(error, RuntimeError) else 'See protected local state; no credentials printed'}), file=sys.stderr)
        sys.exit(1)
