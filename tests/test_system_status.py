from marking_erp.system_routes import CHECKS, public_status


def test_status_allowlist_drops_secrets_paths_and_untyped_checks():
    report = {'ready': True, 'build_sha': 'DSN_PRIVATE_PASSWORD',
              'database_url': 'postgresql://PRIVATE_PASSWORD', 'path': '/private/server',
              'checks': {**dict.fromkeys(CHECKS, True), 'password': 'PRIVATE_PASSWORD', 'mfa_key': 'PRIVATE_KEY'}}
    result = public_status(report)
    assert result['build_sha'] is None and result['checks']['mfa_key'] is None
    assert not result['technical_ready'] and not result['business_integrations_verified']
    assert 'PRIVATE' not in str(result) and '/private' not in str(result)
    assert set(result['checks']) == set(CHECKS)


def test_unknown_or_failed_checks_never_become_ready():
    for report in [{}, {'ready': True, 'checks': None}, {'ready': False, 'checks': dict.fromkeys(CHECKS, True)},
                   {'ready': True, 'checks': dict.fromkeys(CHECKS, 1)}]:
        assert not public_status(report)['technical_ready']
    result = public_status({'ready': True, 'checks': dict.fromkeys(CHECKS, True), 'build_sha': 'a' * 40})
    assert result['technical_ready'] and result['build_sha'] == 'a' * 40
    assert result['components']['true_api_oms'] == 'not_connected'
