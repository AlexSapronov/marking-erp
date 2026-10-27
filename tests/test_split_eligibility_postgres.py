from uuid import uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env, client, login
from test_business_postgres import connections
from test_gs1 import sample, GTIN
from marking_erp.registry import register_product
from marking_erp.business import nk, marking, worker
from marking_erp.operations import DispatchBlocked

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures('fresh_schema')]


def test_split_checks_catalog_before_queue_and_again_before_dispatch(env, engine, monkeypatch):
    value = {'attribute': 'КОЛИЧЕСТВО', 'reads': 0}
    def details(self, *, gtin):
        assert gtin == GTIN
        value['reads'] += 1
        return {'good_id': 42, 'good_attrs': [{'attr_id': 24003, 'attr_value': value['attribute']}]}
    monkeypatch.setattr(nk.Client, 'details', details)
    monkeypatch.setattr(marking.Live, 'info', lambda self, codes: {codes[0]: {'status': 'INTRODUCED', 'ownerInn': self.inn, 'quantityInPack': 10}})
    sent = []
    monkeypatch.setattr(marking.Live, 'oms', lambda self, *a, **kw: sent.append(a) or {'orderId': str(uuid4())})
    with client(env) as c:
        login(c, 'administrator'); connections(c, env, writes_enabled=True, oms_token='fixture-token')
    with engine.begin() as c:
        register_product(c, env[1], GTIN, 'PN-1', '8542399000', source='external_verified', source_ref='fixture', evidence_sha256='0'*64)
    body = {'organization_id': str(env[1]), 'request_id': str(uuid4()), 'raw': sample(37, quantity=10).decode(), 'quantity': 3, 'pn': 'PN-1', 'tnved': '8542399000'}
    with client(env) as c:
        login(c, 'specialist')
        value['attribute'] = 'МАССА'
        response = c.post('/api/splits', json=body)
        assert response.status_code != 200, response.text
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one() == 0
        value['attribute'] = 'КОЛИЧЕСТВО'
        response = c.post('/api/splits', json=body)
        assert response.status_code == 200, response.text
        before = value['reads']
        assert c.post('/api/splits', json=body).status_code == 200
        assert value['reads'] == before
    with engine.connect() as c:
        ticket = dict(c.execute(text('SELECT * FROM external_operations')).mappings().one())
        evidence = c.execute(text("SELECT body->'catalog_eligibility' FROM business_items WHERE kind='split'")).scalar_one()
        assert evidence['good_id'] == 42
    ticket['signature'] = b'fixture-only'
    value['attribute'] = 'МАССА'
    with pytest.raises(DispatchBlocked): worker.BusinessWorker(engine, env[0]).dispatch(ticket)
    assert sent == []
    value['attribute'] = 'КОЛИЧЕСТВО'
    worker.BusinessWorker(engine, env[0]).dispatch(ticket)
    assert len(sent) == 1
    def unavailable(self, **kwargs): raise RuntimeError('fixture transport error')
    monkeypatch.setattr(nk.Client, 'details', unavailable)
    with pytest.raises(DispatchBlocked): worker.BusinessWorker(engine, env[0]).dispatch(ticket)
    assert len(sent) == 1
