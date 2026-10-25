import argparse
import json
from pathlib import Path

from .nk_pilot import PilotError
from .nk_pilot_review import review_pilot, write_review
from .nk_read_contract import ContractError


def main():
    parser = argparse.ArgumentParser(description='Offline NK pilot integrity and card diagnostics; never imports data')
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = review_pilot(args.capture)
        write_review(report, args.output)
    except (ContractError, PilotError) as error:
        parser.exit(1, str(error) + '\n')
    # Card contents belong in private files, not terminal/CI logs.
    print(json.dumps({key: report[key] for key in ('coverage', 'card_count', 'cards_with_issues',
                     'issue_counts', 'warnings', 'receipt_hashes_match', 'contract_verified', 'import_allowed')}))
    parser.exit(2 if report['warnings'] or report['cards_with_issues'] else 0)


if __name__ == '__main__':
    main()
