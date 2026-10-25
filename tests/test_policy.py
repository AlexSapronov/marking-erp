import pytest
from marking_erp.permission_catalog import catalog,dependency_errors
from marking_erp.policy import rules_catalog,validate_value
from marking_erp.seed import GROUPS


def test_all_six_reviewed_profiles_satisfy_dependencies():
    for group in GROUPS:
        assert not dependency_errors({p['id']:'none' if p['defaults'][group]=='-' else p['defaults'][group] for p in catalog()})


def test_rule_defaults_and_strict_numeric_types():
    rules={r['id']:r for r in rules_catalog()}
    assert len(rules)==31
    for r in rules.values():validate_value(r,r['default'])
    for bad in [True,1.5,'900',0,86401]:
        with pytest.raises(ValueError):validate_value(rules['nk.sync.interval_seconds'],bad)
    for bad in [True,float('nan'),float('inf'),.49,1.01]:
        with pytest.raises(ValueError):validate_value(rules['nk.duplicates.similarity'],bad)
    for bad in [False,0]:
        with pytest.raises(ValueError):validate_value(rules['workflow.stop.confirm_number'],bad)


def test_dependency_scope_cannot_be_broader_than_read():
    grants={p['id']:'none' for p in catalog()};grants.update({'nk.cards.create':'all','nk.cards.view':'own'})
    assert dependency_errors(grants)
    grants['nk.cards.view']='all';assert not dependency_errors(grants)
