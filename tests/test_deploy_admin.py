import json
import os
from pathlib import Path
import pytest
from marking_erp.deploy_admin import write_configs,command_environment,SetupError
from marking_erp.service import load_settings


def test_private_pair_uses_one_key_and_encodes_special_passwords(tmp_path):
    root=tmp_path/'config';owner='owner:@$/ secret#';runtime='runtime:%?"secret'
    paths=write_configs(root,'https://erp.company.test','a'*40,owner,runtime)
    assert root.stat().st_mode&0o777==0o700
    for path in paths:assert path.stat().st_mode&0o777==0o600
    first,second=map(load_settings,paths[:2])
    from sqlalchemy.engine import make_url
    assert make_url(first.database_url.get_secret_value()).password==runtime
    assert make_url(second.database_url.get_secret_value()).password==owner
    assert first.mfa_encryption_key==second.mfa_encryption_key
    assert 'server_name erp.company.test;' in paths[2].read_text()
    original=[path.read_bytes() for path in paths]
    with pytest.raises(SetupError):write_configs(root,'https://erp.company.test','b'*40,owner,runtime)
    assert [path.read_bytes() for path in paths]==original


@pytest.mark.parametrize('url',['http://erp.company.test','https://erp.company.test:8443','https://bad;host.test','https://erp.company.test/path'])
def test_invalid_public_origin_writes_nothing(tmp_path,url):
    root=tmp_path/'config'
    with pytest.raises(ValueError) as caught:write_configs(root,url,'a'*40,'private-owner','private-runtime')
    assert 'private-owner' not in str(caught.value) and 'private-runtime' not in str(caught.value)
    assert not (root/'runtime.json').exists()


def test_symlink_and_shared_config_directory_rejected(tmp_path):
    real=tmp_path/'real';real.mkdir(mode=0o700);link=tmp_path/'link';link.symlink_to(real)
    with pytest.raises(SetupError):write_configs(link,'https://erp.test','a'*40,'owner','runtime')
    real.chmod(0o755)
    with pytest.raises(SetupError):write_configs(real,'https://erp.test','a'*40,'owner','runtime')


def test_administrative_environment_ignores_stale_erp_values(tmp_path,monkeypatch):
    paths=write_configs(tmp_path/'config','https://erp.test','a'*40,'owner','runtime')
    monkeypatch.setenv('ERP_DATABASE_URL','WRONG');monkeypatch.setenv('ERP_EXTRA','WRONG')
    result=command_environment(load_settings(paths[1]))
    assert 'marking_erp_owner' in result['ERP_DATABASE_URL'] and 'WRONG' not in result['ERP_DATABASE_URL']
    assert 'ERP_EXTRA' not in result and result['ERP_BUILD_SHA']=='a'*40
    assert result['ERP_PG_BIN_DIR']=='/usr/lib/postgresql/16/bin'
