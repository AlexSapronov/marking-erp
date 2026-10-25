"""Private, bounded evidence capture. No ERP persistence or resume after restart."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

from .nk_read_contract import ContractError, MAX_BODY_BYTES, ReadPass, Window
from .nk_read_transport import ReadError

DEFAULT_BUDGET = 64 * 1024 * 1024
MAX_BUDGET = 256 * 1024 * 1024


class PilotError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_output(output: Path):
    output = Path(output)
    package_root = Path(__file__).resolve().parent.parent
    if (not output.is_absolute() or '..' in output.parts or output == Path('/')
            or output.is_relative_to(package_root)):
        raise PilotError('nk_private_output_path')
    if output.exists() or output.is_symlink():
        raise PilotError('nk_private_output_exists')
    return output


class PrivateCapture:
    def __init__(self, output: Path):
        output = validate_output(output)
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        self.fd = None
        try:
            for part in output.parts[1:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = following
            # Never reuse or erase a previous capture, even an empty directory.
            os.mkdir(output.name, mode=0o700, dir_fd=parent)
            self.fd = os.open(output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            metadata = os.fstat(self.fd)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PilotError('nk_private_output_permissions')
            os.fsync(parent)
        except (OSError, PilotError) as error:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if isinstance(error, PilotError):
                raise
            raise PilotError('nk_private_output_unavailable') from None
        finally:
            os.close(parent)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def write(self, name, body, *, replace=False):
        temporary = '.pending-' + uuid4().hex if replace else name
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self.fd)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except OSError:
            if replace:
                try:
                    os.unlink(temporary, dir_fd=self.fd)
                except OSError:
                    pass
            raise PilotError('nk_capture_write') from None

    def document(self, name, value):
        self.write(name, json.dumps(value, ensure_ascii=False).encode(), replace=True)


def validate_limits(max_pages, max_bytes):
    if type(max_pages) is not int or not 1 <= max_pages <= 400:
        raise PilotError('nk_page_budget')
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BUDGET:
        raise PilotError('nk_byte_budget')


def capture_pilot(client, *, environment: str, window: Window, output: Path,
                  max_pages: int = 1, max_bytes: int = DEFAULT_BUDGET):
    if environment not in ('sandbox', 'production'):
        raise PilotError('nk_environment')
    validate_limits(max_pages, max_bytes)
    state = ReadPass(window)
    manifest = {'format': 1, 'window': {'from_date': window.from_date, 'to_date': window.to_date},
                'page_size': state.page_size, 'pages': []}
    run = {'format': 1, 'run_id': str(uuid4()), 'environment': environment,
           'started_at': _now(), 'updated_at': _now(), 'state': 'in_progress',
           'max_pages': max_pages, 'max_body_bytes': max_bytes, 'saved_body_bytes': 0,
           'received': [], 'error': None, **state.summary()}
    capture = PrivateCapture(output)
    try:
        capture.document('manifest.json', manifest)
        capture.document('run.json', run)
        try:
            while not state.finished and len(state.pages) < max_pages:
                request = state.next_request()
                names = {}

                def observe(path, body):
                    kind = {'/v4/product-list': 'list', '/v3/feed-product': 'details'}[path]
                    if (type(body) is not bytes or len(body) > MAX_BODY_BYTES
                            or run['saved_body_bytes'] + len(body) > max_bytes):
                        raise PilotError('nk_capture_byte_budget')
                    name = f'{kind}-{request.offset}.json'
                    capture.write(name, body)
                    names[kind] = name
                    run['saved_body_bytes'] += len(body)
                    run['received'].append({'file': name, 'sha256': sha256(body).hexdigest(),
                                            'size': len(body), 'received_at': _now()})
                    run['updated_at'] = _now()
                    capture.document('run.json', run)

                evidence = client.read_next(state, observe=observe)
                manifest['pages'].append({'offset': request.offset, 'limit': request.limit,
                                          'listing': names['list'], 'details': names.get('details')})
                capture.document('manifest.json', manifest)
                state = evidence.state
                run.update(state.summary(), updated_at=_now())
                capture.document('run.json', run)
            run.update(state='complete' if state.finished else 'page_limit', updated_at=_now())
        except (ReadError, ContractError, PilotError) as error:
            run.update(state='failed', updated_at=_now(), error={
                'code': str(error), 'status': getattr(error, 'status', None),
                'retryable': getattr(error, 'retryable', False),
                'retry_after': getattr(error, 'retry_after', None)})
        except KeyboardInterrupt:
            run.update(state='interrupted', updated_at=_now())
        capture.document('run.json', run)
        return {key: run[key] for key in ('run_id', 'state', 'coverage', 'page_count',
                'card_count', 'reported_total', 'saved_body_bytes', 'error',
                'atomic_snapshot', 'authenticity_verified', 'erp_imported')}
    finally:
        capture.close()
