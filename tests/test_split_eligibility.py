import pytest
from marking_erp.business.split_eligibility import check
from marking_erp.business.errors import RemoteError


class Catalogue:
    def __init__(self, attrs): self.attrs = attrs
    def details(self, *, gtin):
        assert gtin == '04640638349704'
        return {'good_id': 12, 'good_attrs': self.attrs}


@pytest.mark.parametrize('attrs', [None, [], [{'attr_id': 24003, 'attr_value': 'МАССА'}],
    [{'attr_id': 24003, 'attr_value': None}],
    [{'attr_id': 24003, 'attr_value': 'КОЛИЧЕСТВО'}, {'attr_id': 24003, 'attr_value': 'МАССА'}]])
def test_unconfirmed_quantity_is_rejected(attrs):
    with pytest.raises(RemoteError): check(Catalogue(attrs), '04640638349704')


def test_confirmed_quantity_records_evidence_without_copying_card():
    evidence = check(Catalogue([{'attr_id': '24003', 'attr_value': 'КОЛИЧЕСТВО'}]), '04640638349704')
    assert evidence['value'] == 'КОЛИЧЕСТВО' and evidence['good_id'] == 12
    assert len(evidence['sha256']) == 64 and evidence['checked_at']
    assert 'good_attrs' not in evidence
