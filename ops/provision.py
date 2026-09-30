#!/usr/bin/env python3
"""Run on cube as root, from a copied ops directory. Never replaces live disks."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path('/var/lib/libvirt/images/airlock')
OPS = Path(__file__).resolve().parent
STATE = Path('/srv/airlock')
IMAGE = 'https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-amd64.img'
SHA256 = '46b0dbaffa6950a7da5ff2dc5ed34c46084610b3b6d1fae8f1ec2d7e953984a3'

def run(*args):
    subprocess.run(args, check=True)

started = time.monotonic()
if os.geteuid() != 0:
    raise SystemExit('Run as root on cube')
if (ROOT / 'root.qcow2').exists() or (ROOT / 'data.raw').exists():
    raise SystemExit('Existing Airlock disks: refusing reprovision; deploy code separately')
ROOT.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
if not (STATE / 'admin_ed25519').exists():
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'airlock-cube-operator', '-f', str(STATE / 'admin_ed25519'))
public_key = (STATE / 'admin_ed25519.pub').read_text().strip()
config = {
    'hostname': 'airlock', 'manage_etc_hosts': True,
    'users': [
        {'name': 'airlock-admin', 'groups': ['sudo'], 'shell': '/bin/bash', 'lock_passwd': True, 'sudo': ['ALL=(ALL) NOPASSWD:ALL'], 'ssh_authorized_keys': [public_key]},
        {'name': 'airlock', 'system': True, 'shell': '/usr/sbin/nologin', 'lock_passwd': True},
    ],
    'disable_root': True, 'ssh_pwauth': False, 'package_update': False, 'package_upgrade': False,
    'fs_setup': [{'label': 'airlock-data', 'filesystem': 'ext4', 'device': '/dev/vdb1', 'overwrite': False}],
    'mounts': [['LABEL=airlock-data', '/var/lib/airlock', 'ext4', 'defaults', '0', '2']],
    'write_files': [
        {'path': '/etc/systemd/system/airlock.service', 'content': (OPS / 'airlock.service').read_text()},
        {'path': '/etc/airlock.conf', 'permissions': '0644', 'content': (OPS / 'airlock.conf').read_text()},
        {'path': '/etc/ssh/sshd_config.d/airlock.conf', 'content': 'PasswordAuthentication no\nPermitRootLogin no\nUseDNS no\nAllowUsers airlock-admin\n'},
        {'path': '/etc/sysctl.d/airlock.conf', 'content': 'net.ipv6.conf.all.disable_ipv6=1\nnet.ipv6.conf.default.disable_ipv6=1\n'},
    ],
    'runcmd': [
        ['mkdir', '-p', '/opt/airlock', '/var/lib/airlock'],
        ['chown', 'airlock:airlock', '/var/lib/airlock'],
        ['chmod', '0700', '/var/lib/airlock'],
        ['systemctl', 'disable', '--now', 'apt-daily.timer', 'apt-daily-upgrade.timer', 'systemd-timesyncd.service', 'snapd.socket', 'snapd.service'],
        ['systemctl', 'daemon-reload'], ['systemctl', 'enable', 'airlock.service'], ['sysctl', '--system'],
    ],
}
network = {'version': 2, 'ethernets': {'airlock0': {'match': {'macaddress': '52:54:00:a1:10:02'}, 'set-name': 'airlock0', 'addresses': ['192.168.127.2/30'], 'dhcp4': False, 'dhcp6': False, 'accept-ra': False}}}
Path('/tmp/agents').mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='airlock-provision-', dir='/tmp/agents') as tmp:
    tmp = Path(tmp)
    downloaded = tmp / 'ubuntu.img'
    print('Downloading pinned minimal Ubuntu image', flush=True)
    urllib.request.urlretrieve(IMAGE, downloaded)
    digest = hashlib.file_digest(downloaded.open('rb'), 'sha256').hexdigest()
    if digest != SHA256:
        raise SystemExit(f'Image checksum mismatch: {digest}; no VM disks created')
    run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', str(downloaded), str(ROOT / 'root.qcow2'))
    run('qemu-img', 'resize', str(ROOT / 'root.qcow2'), '8G')
    run('qemu-img', 'create', '-f', 'raw', str(ROOT / 'data.raw'), '2G')
    # Initialize only the newly created blank image. Cloud-init never overwrites
    # an existing partition table or filesystem on the authoritative data disk.
    run('parted', '--script', str(ROOT / 'data.raw'), 'mklabel', 'gpt', 'mkpart', 'primary', 'ext4', '1MiB', '100%')
    (tmp / 'user-data').write_text('#cloud-config\n' + json.dumps(config))
    (tmp / 'meta-data').write_text('instance-id: airlock-20260930\nlocal-hostname: airlock\n')
    (tmp / 'network-config').write_text(json.dumps(network))
    run('cloud-localds', '--network-config=' + str(tmp / 'network-config'), str(ROOT / 'seed.iso'), str(tmp / 'user-data'), str(tmp / 'meta-data'))
run('chown', '-R', 'libvirt-qemu:kvm', str(ROOT))
run('virsh', 'nwfilter-define', str(OPS / 'filter.xml'))
run('virsh', 'net-define', str(OPS / 'network.xml'))
run('virsh', 'net-start', 'airlock-isolated')
run('virsh', 'net-autostart', 'airlock-isolated')
run('virsh', 'define', str(OPS / 'domain.xml'))
run('virsh', 'autostart', 'airlock')
run('virsh', 'start', 'airlock')
print(f'VM provisioned; elapsed_seconds={time.monotonic() - started:.1f}', flush=True)
