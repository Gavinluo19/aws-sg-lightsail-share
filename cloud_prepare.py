#!/usr/bin/env python3
"""One fresh Lightsail instance. Read-only plan by default; no AWS secrets accepted."""
import argparse
import base64
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time

REGION = 'ap-southeast-1'
PORTS = [
    {'fromPort': 22, 'toPort': 22, 'protocol': 'tcp', 'cidrs': [],
     'ipv6Cidrs': [], 'cidrListAliases': ['lightsail-connect']},
    *[{'fromPort': p, 'toPort': p, 'protocol': 'tcp', 'cidrs': ['0.0.0.0/0'],
       'ipv6Cidrs': [], 'cidrListAliases': []} for p in [80, 443]],
    {'fromPort': 51821, 'toPort': 51821, 'protocol': 'udp', 'cidrs': ['0.0.0.0/0'],
     'ipv6Cidrs': [], 'cidrListAliases': []},
]


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def aws(args, service, action, **values):
    command = ['aws', '--profile', args.profile, '--region', args.region,
               '--cli-connect-timeout', '8', '--cli-read-timeout', '30',
               '--no-cli-pager', service, action, '--output', 'json']
    if values:
        with tempfile.TemporaryDirectory(prefix='selfhost-aws-') as tmp:
            path = Path(tmp) / 'input.json'
            write_private(path, values)
            result = subprocess.run(command + ['--cli-input-json', 'file://' + str(path)],
                                    capture_output=True, text=True, timeout=45)
    else:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    if result.returncode:
        match = re.search(r'An error occurred \(([A-Za-z0-9_.-]+)\)', result.stderr)
        code = match.group(1) if match else 'SessionExpired' if 'expired' in result.stderr.lower() else 'ClientOrTransportError'
        raise RuntimeError('AWS call failed: ' + service + '/' + action + ' [' + code + ']; inspect login, permissions and the protected resource ledger')
    return json.loads(result.stdout)


def userdata(ip, args):
    source = Path(args.bootstrap).read_bytes()
    encoded = base64.b64encode(gzip.compress(source, mtime=0)).decode()
    command = ['python3', '/opt/selfhost-vps/server_bootstrap.py', '--apply',
               '--confirm-fresh-server', '--accept-acme-terms', '--public-ip', ip,
               '--email', args.email, '--devices', args.devices,
               '--download-mbps', str(args.download_mbps),
               '--direct-domains', args.direct_domains]
    script = '''#!/bin/bash
set -eu
umask 077
mkdir -p /opt/selfhost-vps
python3 - <<'PYCODE'
import base64,gzip,os,urllib.request,time
from pathlib import Path
ip=__IP__
source=gzip.decompress(base64.b64decode(__ENCODED__))
p=Path('/opt/selfhost-vps/server_bootstrap.py');p.write_bytes(source);p.chmod(0o700)
# Do not request a certificate until the reserved static IPv4 is attached.
for _ in range(180):
 try:
  token=urllib.request.urlopen(urllib.request.Request('http://169.254.169.254/latest/api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=2).read().decode()
  current=urllib.request.urlopen(urllib.request.Request('http://169.254.169.254/latest/meta-data/public-ipv4',headers={'X-aws-ec2-metadata-token':token}),timeout=2).read().decode().strip()
  if current==ip:break
 except Exception:pass
 try:
  egress=urllib.request.urlopen('https://checkip.amazonaws.com',timeout=3).read().decode().strip()
  if egress==ip:break
 except Exception:pass
 time.sleep(5)
else:raise SystemExit('Static IP attachment not confirmed; setup stopped')
PYCODE
__COMMAND__
'''
    script = script.replace('__IP__', repr(ip)).replace('__ENCODED__', repr(encoded))
    script = script.replace('__COMMAND__', shlex.join(command))
    if len(script.encode()) > 16384:
        raise RuntimeError('User data exceeds 16 KiB; use the browser SSH installation path from the guide')
    return script


def ports_match(rows):
    rows = [r for r in rows if r.get('state', 'open') == 'open']
    expected = {(r['protocol'], r['fromPort'], r['toPort']): r for r in PORTS}
    actual = {(r['protocol'], r['fromPort'], r['toPort']): r for r in rows}
    if set(actual) != set(expected):
        return False
    return all(all(sorted(actual[k].get(field, [])) == sorted(expected[k].get(field, []))
                   for field in ['cidrs', 'ipv6Cidrs', 'cidrListAliases']) for k in expected)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', default='selfhost')
    p.add_argument('--region', default=REGION)
    p.add_argument('--name', default='selfhost-sg-01')
    p.add_argument('--expected-account')
    p.add_argument('--email')
    p.add_argument('--devices', default='laptop-01,phone-01')
    p.add_argument('--direct-domains', default='')
    p.add_argument('--download-mbps', type=int, default=0)
    p.add_argument('--bootstrap', default=str(Path(__file__).with_name('server_bootstrap.py')))
    p.add_argument('--apply', action='store_true')
    p.add_argument('--approve-monthly-usd', type=float)
    p.add_argument('--accept-acme-terms', action='store_true')
    p.add_argument('--ledger', default='private-state/cloud-state.json')
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9-]{2,40}', args.name):
        raise RuntimeError('Invalid instance name')
    if not 0 <= args.download_mbps <= 1000:
        raise RuntimeError('Download Mbps must be 0..1000; 0 is adaptive')
    who = aws(args, 'sts', 'get-caller-identity')
    account = who['Account']
    all_bundles = aws(args, 'lightsail', 'get-bundles', includeInactive=False)['bundles']
    bundles = [b for b in all_bundles if b.get('isActive') and b.get('ramSizeInGb') == 1
               and b.get('cpuCount') == 2 and b.get('diskSizeInGb') == 40
               and b.get('price') == 7 and 'LINUX_UNIX' in b.get('supportedPlatforms', [])
               and 'ipv6' not in b['bundleId'].lower()]
    if len(bundles) != 1:
        raise RuntimeError('The expected $7 public-IPv4 bundle is unavailable; review live pricing')
    bundle = bundles[0]
    blueprints = aws(args, 'lightsail', 'get-blueprints', includeInactive=False)['blueprints']
    blueprints = [b for b in blueprints if b.get('isActive') and b.get('blueprintId') == 'ubuntu_24_04']
    if len(blueprints) != 1:
        raise RuntimeError('Ubuntu 24.04 blueprint not available in this region')
    region = next(r for r in aws(args, 'lightsail', 'get-regions', includeAvailabilityZones=True)['regions']
                  if r['name'] == args.region)
    zones = [z['zoneName'] for z in region['availabilityZones'] if z.get('state') == 'available']
    if not zones:
        raise RuntimeError('No available zone found')
    instances = aws(args, 'lightsail', 'get-instances')['instances']
    matching = [i for i in instances if i['name'] == args.name]
    summary = {'mode': 'plan', 'account_suffix': account[-4:], 'region': args.region,
               'instance_name': args.name, 'bundle': bundle['bundleId'],
               'monthly_base_usd': bundle['price'], 'vcpu': bundle['cpuCount'],
               'memory_gb': bundle['ramSizeInGb'], 'disk_gb': bundle['diskSizeInGb'],
               'transfer_gb': bundle.get('transferPerMonthInGb'),
               'existing_name': bool(matching), 'ssh': 'Lightsail browser access only',
               'extra_services': 'none', 'tax_egress_and_credit_terms': 'check separately'}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if not args.apply:
        return
    if args.expected_account != account or args.approve_monthly_usd != 7:
        raise RuntimeError('Owner must confirm the target account and $7 monthly base cost')
    if not args.email or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', args.email):
        raise RuntimeError('Provide an owner contact email for the certificate')
    if not args.accept_acme_terms:
        raise RuntimeError('Owner must review ACME subscriber terms before issuance')
    if matching:
        tags = {t['key']: t.get('value') for t in matching[0].get('tags', [])}
        if tags.get('Project') != 'selfhost-vps':
            raise RuntimeError('The name belongs to another resource; no changes made')
        print(json.dumps({'status': 'existing_owned_instance', 'action': 'inspect its status; do not create a duplicate'}))
        return
    ip_name = args.name + '-ip'
    if any(x['name'] == ip_name for x in aws(args, 'lightsail', 'get-static-ips')['staticIps']):
        raise RuntimeError('Static IP name already exists; inspect it before retrying')
    operation = secrets.token_hex(8)
    ledger = {'operation': operation, 'account': account, 'region': args.region,
              'instance': args.name, 'static_ip': ip_name, 'status': 'starting',
              'instance_created': False, 'firewall_restricted': False}
    write_private(args.ledger, ledger)
    try:
        aws(args, 'lightsail', 'allocate-static-ip', staticIpName=ip_name)
        for _ in range(15):
            try:
                ip = aws(args, 'lightsail', 'get-static-ip', staticIpName=ip_name)['staticIp']['ipAddress']
                ipaddress.IPv4Address(ip)
                break
            except (RuntimeError, KeyError):
                time.sleep(2)
        else:
            raise RuntimeError('Static IP did not become ready')
        ledger['public_ip'] = ip
        write_private(args.ledger, ledger)
        data = userdata(ip, args)
        aws(args, 'lightsail', 'create-instances', instanceNames=[args.name],
            availabilityZone=zones[0], blueprintId=blueprints[0]['blueprintId'],
            bundleId=bundle['bundleId'], ipAddressType='dualstack', userData=data,
            tags=[{'key': 'Project', 'value': 'selfhost-vps'},
                  {'key': 'Operation', 'value': operation}])
        ledger['instance_created'] = True
        for _ in range(25):
            try:
                aws(args, 'lightsail', 'put-instance-public-ports', instanceName=args.name, portInfos=PORTS)
                observed = aws(args, 'lightsail', 'get-instance-port-states', instanceName=args.name)['portStates']
                if ports_match(observed):
                    ledger['firewall_restricted'] = True
                    break
                time.sleep(3)
            except RuntimeError:
                time.sleep(3)
        else:
            raise RuntimeError('Could not restrict cloud firewall; startup will be stopped')
        for _ in range(40):
            instance = aws(args, 'lightsail', 'get-instance', instanceName=args.name)['instance']
            if instance['state']['name'] == 'running':
                break
            time.sleep(3)
        else:
            raise RuntimeError('Instance is not running; inspect the ledger before retry')
        aws(args, 'lightsail', 'update-instance-metadata-options', instanceName=args.name,
            httpTokens='required', httpPutResponseHopLimit=1, httpEndpoint='enabled')
        for _ in range(25):
            options = aws(args, 'lightsail', 'get-instance', instanceName=args.name)['instance'].get('metadataOptions', {})
            if options.get('httpTokens') == 'required' and options.get('httpPutResponseHopLimit') == 1:
                break
            time.sleep(3)
        else:
            raise RuntimeError('IMDSv2 settings were not confirmed; setup remains pending')
        aws(args, 'lightsail', 'attach-static-ip', staticIpName=ip_name, instanceName=args.name)
        for _ in range(25):
            attachment = aws(args, 'lightsail', 'get-static-ip', staticIpName=ip_name)['staticIp']
            if attachment.get('isAttached') is True and attachment.get('attachedTo') == args.name and attachment.get('ipAddress') == ip:
                break
            time.sleep(3)
        else:
            raise RuntimeError('Static IP attachment readback did not match; inspect the resource ledger')
        ports = aws(args, 'lightsail', 'get-instance-port-states', instanceName=args.name)['portStates']
        if not ports_match(ports):
            ledger['firewall_restricted'] = False
            raise RuntimeError('SSH allowlist readback did not match')
        ledger['status'] = 'server_bootstrap_running'
        write_private(args.ledger, ledger)
        print(json.dumps({'status': ledger['status'], 'public_ip': ip,
                          'health_url': 'https://' + ip + '/health',
                          'next': 'wait for valid HTTPS health, then download the protected handoff file via browser SSH',
                          'credentials_on_stdout': False}, ensure_ascii=False))
    except Exception:
        if not ledger['instance_created']:
            # A timeout can hide a successful create. Check this operation's tag before stopping anything.
            try:
                instance = aws(args, 'lightsail', 'get-instance', instanceName=args.name)['instance']
                tags = {t['key']: t.get('value') for t in instance.get('tags', [])}
                ledger['instance_created'] = tags.get('Project') == 'selfhost-vps' and tags.get('Operation') == operation
            except Exception:
                ledger['create_outcome_unconfirmed'] = True
        if ledger['instance_created'] and not ledger['firewall_restricted']:
            try:
                aws(args, 'lightsail', 'stop-instance', instanceName=args.name)
                ledger['instance_stopped_for_safety'] = True
            except Exception:
                ledger['independent_cloud_review_required'] = True
        ledger['status'] = 'needs_review'
        ledger['billing_warning'] = 'Stopped instances still bill; unattached static IPs may bill. Review and remove only resources created by this operation.'
        write_private(args.ledger, ledger)
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'stopped', 'error_type': type(error).__name__,
                          'detail': str(error) if isinstance(error, RuntimeError) else 'Inspect the protected ledger; no credentials printed'}), file=sys.stderr)
        sys.exit(1)
