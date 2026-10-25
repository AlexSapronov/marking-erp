"""Install a verified release into a NEW directory; no DB/service/link changes."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys


class InstallError(ValueError):pass


def verify_bundle(root):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise InstallError('Bundle must be a real directory')
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):raise InstallError('Links/special files are not allowed')
    manifest=json.loads((root/'MANIFEST.json').read_text())
    if set(manifest)!= {'sha','files'} or not re.fullmatch('[a-f0-9]{40}',manifest['sha']):raise InstallError('Invalid manifest')
    if not isinstance(manifest['files'],dict) or not manifest['files']:raise InstallError('Empty manifest')
    actual={str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() and p!=root/'MANIFEST.json'}
    if actual!=set(manifest['files']):raise InstallError('File set differs from manifest')
    for name,digest in manifest['files'].items():
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path)!=name or not re.fullmatch('[a-f0-9]{64}',digest):raise InstallError('Invalid manifest entry')
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise InstallError('Bundle checksum mismatch')
    release=json.loads((root/'release.json').read_text())
    expected={'sha':manifest['sha'],'schema_revision':release.get('schema_revision'),'os':'ubuntu','version':'24.04','machine':'x86_64','python':'3.12'}
    if release!=expected or not isinstance(release['schema_revision'],str):raise InstallError('Unsupported release metadata')
    requirements=(root/'requirements.lock').read_text().splitlines()
    if not requirements or not all(re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+',line) for line in requirements):raise InstallError('Requirements must contain exact pins, no URLs or options')
    if not (root/'wheelhouse').is_dir() or not any((root/'wheelhouse').glob('marking_erp-*.whl')):raise InstallError('Application wheel missing')
    return release


def check_host():
    values=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line and not line.startswith('#'))
    if values.get('ID','').strip('"')!='ubuntu' or values.get('VERSION_ID','').strip('"')!='24.04':raise InstallError('This bundle requires Ubuntu 24.04; no OS upgrade is performed')
    if platform.machine()!='x86_64' or sys.version_info[:2]!=(3,12):raise InstallError('Requires x86_64 and Python 3.12')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--bundle',type=Path,required=True);args=parser.parse_args()
    target=None;created=False
    try:
        if os.geteuid()!=0:raise InstallError('Run with sudo')
        check_host();release=verify_bundle(args.bundle)
        os.umask(0o022)  # Installed code must be readable/executable by the service user.
        parent=Path('/opt/marking-erp/releases')
        if any(p.is_symlink() for p in [parent,*parent.parents]):raise InstallError('Release path contains a symlink')
        parent.mkdir(parents=True,mode=0o755,exist_ok=True)
        for p in [parent,parent.parent]:
            if p.stat().st_uid!=0 or p.stat().st_mode&0o022:raise InstallError('Release directories must be root-owned and not group/world writable')
        target=parent/release['sha']
        if target.exists():raise InstallError('Release already exists; nothing overwritten')
        shutil.copytree(args.bundle,target);created=True
        subprocess.run([sys.executable,'-m','venv',str(target/'venv')],check=True)
        python=target/'venv/bin/python'
        subprocess.run([str(python),'-m','pip','install','--no-index','--only-binary=:all:','--no-cache-dir','--find-links',str(target/'wheelhouse'),'--requirement',str(target/'requirements.lock')],check=True)
        subprocess.run([str(python),'-m','pip','check'],check=True)
        code="from marking_erp.migrate import schema_head; from importlib.resources import files; import sys; assert schema_head()==sys.argv[1]; assert files('marking_erp').joinpath('static/documents.js').is_file()"
        subprocess.run([str(python),'-c',code,release['schema_revision']],check=True)
        print('Installed:',target)
        print('No database, services or current symlink were changed.')
    except (ValueError,OSError,subprocess.CalledProcessError) as error:
        if created and target:shutil.rmtree(target)
        parser.exit(1,'Installation failed: '+(str(error) if isinstance(error,InstallError) else 'invalid bundle or failed offline installation')+'\n')


if __name__=='__main__':main()
