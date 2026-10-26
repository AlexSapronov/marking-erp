import json
import os
from pathlib import Path
import sys
import shutil
import subprocess
import pytest
from cryptography.fernet import Fernet
from marking_erp import service
from marking_erp.runtime_access import _identifier


def config(tmp_path, **overrides):
    data = {'database_url': 'postgresql+psycopg://runtime:PRIVATE_PASSWORD@127.0.0.1/erp',
            'mfa_encryption_key': Fernet.generate_key().decode(), 'public_url': 'https://erp.example',
            'secure_cookie': True, 'file_storage_path': str(tmp_path / 'files'), 'build_sha': 'a' * 40,
            **overrides}
    path = tmp_path / 'runtime.json'
    path.write_text(json.dumps(data));path.chmod(0o600)
    return path


def test_private_config_overrides_environment_without_echo(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setenv('ERP_DATABASE_URL', 'invalid')
    settings = service.load_settings(path)
    assert settings.database_url.get_secret_value().endswith('/erp')
    assert 'PRIVATE_PASSWORD' not in repr(settings)


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o660])
def test_shared_config_rejected(tmp_path, mode):
    path = config(tmp_path);path.chmod(mode)
    with pytest.raises(service.ServiceError, match='service_config_permissions'):
        service.load_settings(path)


def test_invalid_config_and_duplicate_keys_never_echo_password(tmp_path):
    path = config(tmp_path)
    for data in [b'{"database_url":"PRIVATE_PASSWORD","database_url":2}', b'PRIVATE_PASSWORD', b'\xff', b' ' * 65537]:
        path.write_bytes(data)
        with pytest.raises(service.ServiceError) as caught:
            service.load_settings(path)
        assert 'PRIVATE_PASSWORD' not in str(caught.value)


def test_symlink_config_and_nonregular_file_rejected(tmp_path):
    path = config(tmp_path);link = tmp_path / 'link';link.symlink_to(path)
    with pytest.raises(service.ServiceError):service.load_settings(link)
    fifo = tmp_path / 'pipe';os.mkfifo(fifo)
    with pytest.raises(service.ServiceError):service.load_settings(fifo)


def test_file_storage_requires_private_real_owned_directory(tmp_path):
    path = tmp_path / 'files';path.mkdir(mode=0o700)
    assert service.filesystem_ready(path)
    path.chmod(0o755);assert not service.filesystem_ready(path)
    path.chmod(0o700)
    link = tmp_path / 'link';link.symlink_to(path, target_is_directory=True)
    assert not service.filesystem_ready(link)
    assert not service.filesystem_ready(path / 'missing')


def test_identifiers_fail_before_any_sql():
    for value in ['a;drop role x', 'a"b', 'PUBLIC', '', 'a' * 64, None]:
        with pytest.raises(ValueError):_identifier(value)


def test_credential_directory_used_without_shell_or_environment_secrets(tmp_path, monkeypatch, capsys):
    path = config(tmp_path);path.rename(tmp_path / 'erp-config')
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['service', 'check'])
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(service, 'database_engine', lambda _: Engine())
    monkeypatch.setattr(service, 'inspect_runtime', lambda *_: {'ready': True, 'checks': {}})
    service.main()
    assert json.loads(capsys.readouterr().out)['ready']


def test_failed_preflight_does_not_start_server(tmp_path, monkeypatch, capsys):
    path = config(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['service', '--config', str(path), 'serve'])
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(service, 'database_engine', lambda _: Engine())
    monkeypatch.setattr(service, 'inspect_runtime', lambda *_: {'ready': False})
    import uvicorn
    monkeypatch.setattr(uvicorn, 'run', lambda *_args, **_kwargs: pytest.fail('must not start'))
    with pytest.raises(SystemExit) as caught:service.main()
    assert caught.value.code == 1 and capsys.readouterr().err == 'service_preflight_failed\n'


def test_systemd_unit_syntax_with_placeholder_executable(tmp_path):
    analyze = shutil.which('systemd-analyze')
    if not analyze:
        if os.environ.get('CI'):pytest.fail('Ubuntu CI requires systemd-analyze')
        pytest.skip('systemd-analyze unavailable')
    unit = Path(__file__).resolve().parents[1] / 'deploy/marking-erp.service'
    target = tmp_path / 'marking-erp.service'
    target.write_text(unit.read_text().replace('/opt/marking-erp/current/venv/bin/marking-erp-service', '/usr/bin/true'))
    result = subprocess.run([analyze, 'verify', '--man=no', str(target)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('change', ['valid','foreign_user','extra_group','owner_write','mask_write','other_read','truncated','version'])
def test_systemd_acl_accepts_only_root_and_service_read_access(monkeypatch,change):
    import struct
    uid=12345;undefined=0xffffffff
    entries=[(1,4,undefined),(2,4,uid),(4,0,undefined),(16,4,undefined),(32,0,undefined)]
    if change=='foreign_user':entries[1]=(2,4,54321)
    if change=='extra_group':entries.insert(3,(8,4,123))
    if change=='owner_write':entries[0]=(1,6,undefined)
    if change=='mask_write':entries[3]=(16,6,undefined)
    if change=='other_read':entries[4]=(32,4,undefined)
    value=struct.pack('<I',3 if change=='version' else 2)+b''.join(struct.pack('<HHI',*entry) for entry in entries)
    if change=='truncated':value=value[:-1]
    monkeypatch.setattr(os,'getxattr',lambda *_:value)
    assert service.credential_acl_is_private(0,uid)==(change=='valid')


def test_acl_mode_is_only_allowed_for_systemd_credential_context(tmp_path,monkeypatch):
    import struct
    path=config(tmp_path);path.chmod(0o440);undefined=0xffffffff
    value=struct.pack('<I',2)+b''.join(struct.pack('<HHI',*entry) for entry in [(1,4,undefined),(2,4,os.getuid()),(4,0,undefined),(16,4,undefined),(32,0,undefined)])
    monkeypatch.setattr(os,'getxattr',lambda *_:value)
    with pytest.raises(service.ServiceError,match='service_config_permissions'):service.load_settings(path)
    assert service.load_settings(path,systemd_credential=True).build_sha=='a'*40
