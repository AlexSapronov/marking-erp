"""Local audit probes, NOT ERP regression tests. Never contacts production APIs.

Usage: python scripts/reproduce_audit.py /path/to/source-snapshots
Dependencies: source applications' test dependencies. Runs each source in a separate
process; databases/configuration are confined to TemporaryDirectory.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

PROBES = {
    "S-004": ("km-split", """
from km_split.codes import division_entry,parse,GS,RS
source='0104640638345218215DEMO000000000000000'+GS+'93dGVz'+GS+'30100'
new='0104640638345218215SECOND0000000000000'+GS+'93abcd'+GS+'3030'
entry=division_entry(new,source)
source_in_report=parse(entry.split(RS)[1])
print(json.dumps({'source_ai30_in_report':source_in_report.quantity,'expected_source_ai30_in_report':None}))
assert source_in_report.quantity==100
"""),
    "W-001": ("gis-trueapi", """
import os, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as temp:
    os.environ['GIS_MODE']='mock'
    os.environ['WAREHOUSE_DB']=str(Path(temp)/'warehouse.sqlite3')
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as client:
        response=client.get('/api/warehouse/documents')
        print(json.dumps({'anonymous_list_http':response.status_code}))
        assert response.status_code==200
"""),
    "W-002": ("gis-trueapi", """
from warehouse_service import receipt_results
result=receipt_results({
 'expected':[{'cis':'synthetic','gtin':'synthetic','full_code':'synthetic-full','quantity':3}],
 'lines':[{'gtin':'synthetic','quantity':10}],
 'scans':[{'id':'scan','cis':'synthetic','gtin':'synthetic','full_code':'synthetic-full'}]})
print(json.dumps({'ok':result['ok'],'remaining':result['rows'][0]['remaining']}))
assert result['ok'] is True and result['rows'][0]['remaining']==7
"""),
    "S-001": ("km-split", """
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from km_split.config import Settings
from main import create_app
with tempfile.TemporaryDirectory() as temp:
    settings=Settings(data=Path(temp)/'demo',mode='demo',user='',password='',organizations=[],root=Path(temp))
    with TestClient(create_app(settings)) as client:
        read=client.get('/api/settings')
        config=read.json()
        response=client.post('/api/settings',headers={'X-Requested-With':'km-split'},json={
          'mode':'demo','organizations':[],'revision':config['revision'],'file_signing_enabled':True})
        print(json.dumps({'anonymous_settings_read_http':read.status_code,'anonymous_settings_write_http':response.status_code}))
        assert read.status_code==200 and response.status_code==200
"""),
}

def main():
    if len(sys.argv)!=2:
        raise SystemExit('Provide source snapshot root; no network retrieval is performed.')
    root=Path(sys.argv[1]).resolve()
    manifest=json.loads((Path(__file__).resolve().parents[1]/'docs/audit/retrieved-manifest.json').read_text())
    import hashlib
    checked=0
    for f in manifest:
        path=root/f['repository'].split('/')[1]/f['path']
        if path.suffix!='.py':continue
        data=path.read_bytes()
        actual=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if actual!=f['blob_sha']:raise SystemExit('Source snapshot mismatch: '+str(path))
        checked+=1
    results=[]
    for finding,(repo,code) in PROBES.items():
        env=os.environ.copy();env['PYTHONPATH']=str(root/repo)
        run=subprocess.run([sys.executable,'-c','import json\n'+code],cwd=root/repo,env=env,capture_output=True,text=True,timeout=30)
        if run.returncode:
            raise SystemExit(f'{finding}: probe failed\n{run.stderr}')
        results.append({'finding':finding,'observed':json.loads(run.stdout.strip())})
    print(json.dumps({'verified_python_sources':checked,'probes':results},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
