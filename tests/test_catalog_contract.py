import pytest
from fastapi import HTTPException
from marking_erp.business.nk import Client,build_entry
from marking_erp.business.errors import RemoteError


def test_schema_tries_only_actual_remote_categories_after_404(monkeypatch):
    seen=[]
    def call(self,method,path,params=None,body=None):
        seen.append(params['cat_id'])
        if params['cat_id']==100:raise RemoteError('not found',status=404)
        return [{'attr_id':13914,'attr_name':'Модель'}]
    monkeypatch.setattr(Client,'call',call)
    result=Client({}).schema(999,{'categories':[{'cat_id':100},{'cat_id':200}]})
    assert seen==[100,200] and result[0]['attr_id']==13914


def test_existing_card_sends_changed_attributes_and_preserves_read_only_values():
    schema=[{'attr_id':13914,'attr_name':'Модель','attr_type':'m'},{'attr_id':2504,'attr_name':'Товарный знак','attr_type':'m'},{'attr_id':99,'attr_name':'Служебное','attr_type':'b'}]
    raw={'good_id':7,'good_attrs':[{'attr_id':13914,'attr_value':'PN-OLD'},{'attr_id':2504,'attr_value':'Brand'},{'attr_id':99,'attr_value':'keep'}]}
    data={'pn':'PN-NEW','gtin':'04640638349704','tnved':'8542399000','name':'New name','brand':'Brand','category_id':1,'attributes':{'13914':'PN-OLD','2504':'Brand','99':'keep'},'external':{'good_id':7,'raw':raw}}
    entry=build_entry(data,schema)
    assert entry['good_id']==7 and entry['good_attrs']==[{'attr_id':13914,'attr_value':'PN-NEW'}]
    data['attributes']['99']='modified'
    with pytest.raises(HTTPException):build_entry(data,schema)


def test_quantity_attribute_rejects_nonfinite_values_and_missing_unit():
    schema=[{'attr_id':13914,'attr_name':'Модель','attr_type':'m'},{'attr_id':10,'attr_name':'Масса','attr_type':'m','attr_field_type':'number','attr_value_type':['г']}]
    data={'pn':'PN','gtin':'04640638349704','tnved':'8542399000','name':'Name','brand':'Brand','category_id':1,'attributes':{'10':'NaN','10::unit':'г'}}
    with pytest.raises(HTTPException):build_entry(data,schema)
    data['attributes']={'10':'2'}
    with pytest.raises(HTTPException):build_entry(data,schema)
    data['attributes']['10::unit']='г'
    assert next(a for a in build_entry(data,schema)['good_attrs'] if a['attr_id']==10)['attr_value_type']=='г'
