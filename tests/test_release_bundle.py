import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('install_release',Path(__file__).parents[1]/'deploy/install_release.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)


def bundle(tmp_path):
    root=tmp_path/'bundle';root.mkdir();(root/'wheelhouse').mkdir()
    (root/'wheelhouse/marking_erp-0.1.0-py3-none-any.whl').write_bytes(b'fixture wheel')
    (root/'requirements.lock').write_text('marking-erp==0.1.0\n')
    (root/'release.json').write_text(json.dumps({'sha':'a'*40,'schema_revision':'0006_bm_documents','os':'ubuntu','version':'24.04','machine':'x86_64','python':'3.12'}))
    manifest(root);return root


def manifest(root):
    hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and p!=root/'MANIFEST.json'}
    (root/'MANIFEST.json').write_text(json.dumps({'sha':'a'*40,'files':hashes}))


def test_valid_bundle_and_tampering(tmp_path):
    root=bundle(tmp_path);assert installer.verify_bundle(root)['sha']=='a'*40
    (root/'requirements.lock').write_text('tampered')
    with pytest.raises(installer.InstallError,match='checksum'):installer.verify_bundle(root)


def test_untracked_files_and_links_are_rejected(tmp_path):
    root=bundle(tmp_path);(root/'extra').write_text('untracked')
    with pytest.raises(installer.InstallError,match='File set'):installer.verify_bundle(root)
    (root/'extra').unlink();(root/'link').symlink_to(root/'release.json')
    with pytest.raises(installer.InstallError,match='Links'):installer.verify_bundle(root)


@pytest.mark.parametrize('requirement',['pkg @ https://example.test/pkg.whl','--index-url https://example.test','pkg>=1'])
def test_network_requirements_not_allowed(tmp_path,requirement):
    root=bundle(tmp_path);(root/'requirements.lock').write_text(requirement+'\n');manifest(root)
    with pytest.raises(installer.InstallError,match='exact pins'):installer.verify_bundle(root)


def test_path_traversal_and_wrong_platform_rejected(tmp_path):
    root=bundle(tmp_path);m=json.loads((root/'MANIFEST.json').read_text());m['files']['../outside']='0'*64
    (root/'MANIFEST.json').write_text(json.dumps(m))
    with pytest.raises(installer.InstallError):installer.verify_bundle(root)
    value=json.loads((root/'release.json').read_text());value['machine']='aarch64';(root/'release.json').write_text(json.dumps(value));manifest(root)
    with pytest.raises(installer.InstallError,match='Unsupported'):installer.verify_bundle(root)
