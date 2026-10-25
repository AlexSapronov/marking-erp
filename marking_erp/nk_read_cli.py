"""Check a private manifest of captured HTTP-200 response bodies, offline."""
import argparse
import json
import os
from pathlib import Path
import stat

from .nk_read_contract import ContractError, MAX_BODY_BYTES, ReadPass, Window, decode_json


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ContractError('capture_file_type')
            body = stream.read(MAX_BODY_BYTES + 1)
        if len(body) > MAX_BODY_BYTES:
            raise ContractError('body_size')
        return body
    except OSError:
        raise ContractError('capture_file_unreadable') from None


def _body(directory, name):
    # Captures are flat private folders. No paths outside the manifest directory.
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name or '\x00' in name
            or (directory / name).is_symlink()):
        raise ContractError('capture_file_name')
    return _read(directory / name)


def check_capture(manifest: Path, *, observe=None):
    data = decode_json(_read(manifest))
    if (not isinstance(data, dict) or type(data.get('format')) is not int
            or data['format'] != 1 or set(data) != {'format', 'window', 'page_size', 'pages'}):
        raise ContractError('capture_manifest')
    window = data['window']
    if not isinstance(window, dict) or set(window) != {'from_date', 'to_date'}:
        raise ContractError('capture_window')
    result = ReadPass(Window(**window), data['page_size'])
    pages = data['pages']
    if not isinstance(pages, list) or len(pages) > 10000:
        raise ContractError('capture_pages')
    evidence = []
    for page in pages:
        if (not isinstance(page, dict) or set(page) != {'offset', 'limit', 'listing', 'details'}
                or type(page['offset']) is not int or type(page['limit']) is not int):
            raise ContractError('capture_page')
        request = result.next_request()
        if (page['offset'], page['limit']) != (request.offset, request.limit):
            raise ContractError('capture_page_order')
        listing = _body(manifest.parent, page['listing'])
        details = _body(manifest.parent, page['details']) if page['details'] is not None else None
        result = result.accept(listing, details)
        if observe is not None:
            observe(page, listing, details)
        checked = result.pages[-1]
        evidence.append({'offset': request.offset, 'limit': request.limit,
                         'listing_sha256': checked.listing_sha256,
                         'details_sha256': checked.details_sha256})
    return {**result.summary(), 'evidence': evidence}


def main():
    parser = argparse.ArgumentParser(description='Offline NK read coverage check; no network or ERP import')
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args()
    try:
        summary = check_capture(args.manifest)
    except ContractError as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(summary, ensure_ascii=False))
    if summary['coverage'] != 'bounded_pass':
        parser.exit(2)


if __name__ == '__main__':
    main()
