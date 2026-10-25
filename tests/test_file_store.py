import os
from pathlib import Path
import pytest
from marking_erp.file_store import FileStore,StorageError


def test_private_immutable_files_exact_bytes_and_hash(tmp_path):
    store=FileStore(tmp_path/'private');data=b'01\x1d30\x00\xff';a=store.write(data);b=store.write(data)
    assert a['id']!=b['id'] and a['sha256']==b['sha256'] and store.read(a)==data
    assert os.stat(store.root/a['storage_key']).st_mode&0o777==0o600
    assert not (store.root/a['storage_key']).is_symlink()
    (store.root/a['storage_key']).write_bytes(b'corrupted')
    with pytest.raises(StorageError):store.read(a)


def test_path_traversal_and_symlinks_are_rejected(tmp_path):
    store=FileStore(tmp_path/'private');meta=store.write(b'known data')
    with pytest.raises(StorageError):store.read({**meta,'storage_key':'../../secret'})
    path=store.root/meta['storage_key'];path.unlink();path.symlink_to(tmp_path/'outside')
    with pytest.raises(StorageError):store.read(meta)
    other=tmp_path/'link';other.symlink_to(store.root,target_is_directory=True)
    with pytest.raises(StorageError):FileStore(other).write(b'new')


def test_empty_and_excessive_files_fail(tmp_path):
    for data in [b'',b'x'*(10*1024*1024+1)]:
        with pytest.raises(ValueError):FileStore(tmp_path).write(data)
