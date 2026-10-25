import argparse
import getpass
import json
from pathlib import Path
import sys
import warnings

from pydantic import SecretStr

from .nk_pilot import DEFAULT_BUDGET, PilotError, capture_pilot, validate_limits, validate_output
from .nk_read_contract import ContractError, Window
from .nk_read_transport import NkReadClient, ReadError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # An unsupported --token argument must not be reflected to stderr.
        self.exit(2, 'nk_arguments: use --help\n')


def main():
    parser = _Parser(description='Bounded private NK read pilot; no ERP import or external writes')
    parser.add_argument('--environment', required=True, choices=('sandbox', 'production'))
    parser.add_argument('--from-date', required=True)
    parser.add_argument('--to-date', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-pages', type=int, default=1)
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_BUDGET)
    args = parser.parse_args()
    try:
        window = Window(args.from_date, args.to_date)
        validate_output(args.output)
        validate_limits(args.max_pages, args.max_bytes)
        if not sys.stdin.isatty():
            raise PilotError('nk_token_requires_tty')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            token = SecretStr(getpass.getpass('Токен ГИС МТ (скрытый ввод): '))
        with NkReadClient(environment=args.environment, token=token) as reader:
            result = capture_pilot(reader, environment=args.environment, window=window,
                                   output=args.output, max_pages=args.max_pages, max_bytes=args.max_bytes)
    except (PilotError, ContractError, ReadError) as error:
        parser.exit(1, str(error) + '\n')
    except (EOFError, getpass.GetPassWarning):
        parser.exit(1, 'nk_token_requires_tty\n')
    except KeyboardInterrupt:
        parser.exit(130)
    print(json.dumps(result, ensure_ascii=False))
    parser.exit({'complete': 0, 'page_limit': 2, 'failed': 1, 'interrupted': 130}[result['state']])


if __name__ == '__main__':
    main()
