"""Build an offline wheelhouse pinned to versions in the already-tested CI env."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


def run(*args,**kw):return subprocess.run([str(a) for a in args],check=True,**kw)


def main():
    p=argparse.ArgumentParser();p.add_argument('--sha',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    if not re.fullmatch('[a-f0-9]{40}',a.sha):p.error('Full source SHA required')
    if sys.version_info[:2]!=(3,12):p.error('Python 3.12 required')
    repo=Path(__file__).resolve().parents[1];a.output.mkdir(parents=True,exist_ok=True)
    from marking_erp.migrate import schema_head
    with tempfile.TemporaryDirectory(prefix='erp-release-') as temporary:
        tmp=Path(temporary);bundle=tmp/('marking-erp-'+a.sha);bundle.mkdir();wheelhouse=bundle/'wheelhouse';wheelhouse.mkdir()
        constraints=tmp/'tested-constraints.txt'
        result=run(sys.executable,'-m','pip','freeze','--exclude-editable',capture_output=True,text=True)
        pins=[line for line in result.stdout.splitlines() if re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+',line) and not line.lower().startswith('marking-erp==')]
        constraints.write_text('\n'.join(pins)+'\n')
        run(sys.executable,'-m','pip','wheel','--no-deps','--wheel-dir',wheelhouse,repo)
        wheel=next(wheelhouse.glob('marking_erp-*.whl'))
        resolve=tmp/'resolve';run(sys.executable,'-m','venv',resolve);python=resolve/'bin/python'
        run(python,'-m','pip','install','--constraint',constraints,wheel)
        metadata_script='''import importlib.metadata,json
print(json.dumps(sorted([d.metadata['Name']+'=='+d.version for d in importlib.metadata.distributions() if d.metadata['Name'].lower() not in ('pip','setuptools','wheel')],key=str.lower)))'''
        pins=json.loads(run(python,'-c',metadata_script,capture_output=True,text=True).stdout)
        if not all(re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+',line) for line in pins):raise ValueError('Non-pinned requirement')
        lock=bundle/'requirements.lock';lock.write_text('\n'.join(pins)+'\n')
        dependencies=tmp/'dependencies.txt';dependencies.write_text('\n'.join(line for line in pins if not line.lower().startswith('marking-erp=='))+'\n')
        run(python,'-m','pip','download','--only-binary=:all:','--dest',wheelhouse,'--requirement',dependencies)
        for name in ['marking-erp.service','nginx.conf.example','runtime.example.json']:
            (bundle/'deploy').mkdir(exist_ok=True);shutil.copy2(repo/'deploy'/name,bundle/'deploy'/name)
        shutil.copy2(repo/'deploy/install_release.py',bundle/'install-release.py')
        shutil.copy2(repo/'docs/DEPLOY-VPS.md',bundle/'DEPLOY-VPS.md')
        shutil.copy2(repo/'docs/DELIVERY-STATUS.md',bundle/'DELIVERY-STATUS.md')
        shutil.copy2(repo/'docs/MVP-WAREHOUSE.md',bundle/'MVP-WAREHOUSE.md')
        shutil.copy2(repo/'docs/BUSINESS-MVP.md',bundle/'BUSINESS-MVP.md')
        shutil.copytree(repo/'deploy/windows',bundle/'deploy/windows')
        release={'sha':a.sha,'schema_revision':schema_head(),'os':'ubuntu','version':'24.04','machine':'x86_64','python':'3.12'}
        (bundle/'release.json').write_text(json.dumps(release,indent=2)+'\n')
        hashes={str(path.relative_to(bundle)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(bundle.rglob('*')) if path.is_file()}
        (bundle/'MANIFEST.json').write_text(json.dumps({'sha':a.sha,'files':hashes},sort_keys=True,indent=2)+'\n')
        target=a.output/(bundle.name+'.tar.gz')
        with tarfile.open(target,'w:gz') as tar:tar.add(bundle,arcname=bundle.name)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        (a.output/'SHA256SUMS').write_text(digest+'  '+target.name+'\n')
        print('Release:',a.sha,'Schema:',schema_head(),'Wheels:',len(list(wheelhouse.glob('*.whl'))))
        print('SHA256:',digest)


if __name__=='__main__':main()
