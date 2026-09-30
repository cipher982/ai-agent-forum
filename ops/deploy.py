#!/usr/bin/env python3
"""Deploy a compiled binary and units without replacing VM disks or forum data."""
import hashlib
from pathlib import Path
import secrets
import shlex
import subprocess
import time

REPO = Path(__file__).resolve().parent.parent
OPS = REPO / 'ops'
KEY = '/srv/airlock/admin_ed25519'
KNOWN = '/srv/airlock/known_hosts'
GUEST = 'airlock-admin@192.168.127.2'
SSH = ['ssh', '-i', KEY, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={KNOWN}']
SCP = ['scp', '-i', KEY, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={KNOWN}']

def host(command):
    return subprocess.run(['ssh', 'cube', command], check=True, text=True, capture_output=False)

def guest(command):
    return host(shlex.join(['sudo', *SSH, GUEST, command]))

started = time.monotonic()
binary = REPO / 'airlock'
if not binary.is_file():
    raise SystemExit('Run bun run build first')
identifier = secrets.token_hex(8)
stage = f'/tmp/agents/airlock-deploy-{identifier}'
guest_files = [f'/tmp/agents/airlock-{identifier}-{name}' for name in ['binary', 'service', 'conf']]
files = ['airlock', 'airlock.service', 'airlock.conf', 'airlock-ingress.service']
try:
    host(shlex.join(['mkdir', '-p', stage]))
    subprocess.run(['scp', str(binary), *(str(OPS / name) for name in files[1:]), f'cube:{stage}/'], check=True)
    guest('mkdir -p /tmp/agents')
    for name, remote in zip(files[:3], guest_files):
        host(shlex.join(['sudo', *SCP, f'{stage}/{name}', f'{GUEST}:{remote}']))
    guest(' && '.join([
        shlex.join(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0755', guest_files[0], '/opt/airlock/airlock.next']),
        'sudo mv /opt/airlock/airlock.next /opt/airlock/airlock',
        shlex.join(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0644', guest_files[1], '/etc/systemd/system/airlock.service']),
        shlex.join(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0644', guest_files[2], '/etc/airlock.conf']),
        'sudo systemctl daemon-reload', 'sudo systemctl enable airlock', 'sudo systemctl restart airlock',
    ]))
    guest('python3 -c ' + shlex.quote("import urllib.request,time\nfor attempt in range(30):\n try:\n  response=urllib.request.urlopen('http://127.0.0.1:8080/airlock/api/threads',timeout=2)\n  assert response.status==200\n  print(response.read().decode());break\n except OSError:\n  if attempt==29: raise\n  time.sleep(.2)"))
    host(' && '.join([
        shlex.join(['sudo', 'install', '-o', 'root', '-g', 'root', '-m', '0644', f'{stage}/airlock-ingress.service', '/etc/systemd/system/airlock-ingress.service']),
        'sudo systemctl daemon-reload', 'sudo systemctl enable --now airlock-ingress.service',
    ]))
    digest = hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest()
    print(f'Deployed Airlock binary_sha256={digest} elapsed_seconds={time.monotonic()-started:.1f}')
finally:
    guest(shlex.join(['rm', '-f', '--', *guest_files]))
    host(shlex.join(['rm', '-f', '--', *(f'{stage}/{name}' for name in files)]))
    host(shlex.join(['rmdir', stage]))
