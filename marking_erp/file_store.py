"""Private immutable storage; no client paths, webroot, overwrites or hash-only trust."""
from pathlib import Path
from hashlib import sha256
import os
import re
from uuid import uuid4

MAX_FILE=10*1024*1024

class StorageError(Exception):pass


class FileStore:
    def __init__(self,root):self.root=Path(root)

    def _directory(self,prefix,create=False):
        # Reject symlinks in every existing component, including the configured root.
        path=self.root.absolute()
        for component in [*reversed(path.parents),path]:
            if component.is_symlink():raise StorageError('Недоступное хранилище')
        if create:path.mkdir(parents=True,exist_ok=True,mode=0o700)
        sub=path/prefix
        if sub.is_symlink():raise StorageError('Недоступное хранилище')
        if create:sub.mkdir(mode=0o700,exist_ok=True)
        return sub

    def write(self,data):
        if type(data) is not bytes or not 1<=len(data)<=MAX_FILE:raise ValueError('Недопустимый размер файла')
        id=uuid4();name=id.hex+'.bin';key=id.hex[:2]+'/'+name
        path=self._directory(id.hex[:2],True)/name
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        except OSError:raise StorageError('Не удалось сохранить файл') from None
        return {'id':id,'storage_key':key,'sha256':sha256(data).hexdigest(),'size':len(data)}

    def read(self,metadata):
        key=metadata['storage_key']
        if not re.fullmatch(r'[a-f0-9]{2}/[a-f0-9]{32}\.bin',key):raise StorageError('Недоступный файл')
        directory=self._directory(key[:2]);path=directory/key.split('/')[1]
        try:
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as f:data=f.read(MAX_FILE+1)
        except OSError:raise StorageError('Недоступный файл') from None
        if len(data)!=metadata['size'] or not 1<=len(data)<=MAX_FILE or sha256(data).hexdigest()!=metadata['sha256']:raise StorageError('Нарушена целостность файла')
        return data
