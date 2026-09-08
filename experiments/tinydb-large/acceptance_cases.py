"""Fixed acceptance for opt-in deep read isolation across TinyDB and Table."""
from copy import deepcopy
import json
import pytest
from tinydb import TinyDB, Query
from tinydb.table import Table, Document
from tinydb.storages import MemoryStorage, JSONStorage
from tinydb.middlewares import CachingMiddleware

PAYLOAD={'active':True,'profile':{'name':'Ada','tags':['safe'],'settings':{'enabled':True}}}

@pytest.fixture(params=['memory','json','cached-memory'])
def database(request,tmp_path):
    if request.param=='json':
        db=TinyDB(tmp_path/'db.json',storage=JSONStorage,copy_on_read=True)
    elif request.param=='cached-memory':
        db=TinyDB(storage=CachingMiddleware(MemoryStorage),copy_on_read=True)
    else:
        db=TinyDB(storage=MemoryStorage,copy_on_read=True)
    yield db
    db.close()

def read_one(db,api,doc_id):
    if api=='get-id': return db.get(doc_id=doc_id)
    if api=='get-query': return db.get(Query().active==True)
    if api=='get-ids': return db.get(doc_ids=[doc_id])[0]
    if api=='search': return db.search(Query().active==True)[0]
    if api=='all': return db.all()[0]
    if api=='iter': return next(iter(db))
    raise AssertionError(api)

@pytest.mark.parametrize('api',['get-id','get-query','get-ids','search','all','iter'])
def test_all_read_paths_are_deeply_isolated(database,api):
    doc_id=database.insert(deepcopy(PAYLOAD))
    doc=read_one(database,api,doc_id)
    assert isinstance(doc,Document) and doc.doc_id==doc_id
    doc['profile']['tags'].append('corrupt')
    doc['profile']['settings']['enabled']=False
    doc['active']=False
    doc.doc_id=999
    again=read_one(database,api,doc_id)
    assert again==PAYLOAD
    assert again.doc_id==doc_id
    assert database.get(doc_id=doc_id)==PAYLOAD


def test_cached_results_stay_isolated_without_disabling_cache(database):
    database.insert(deepcopy(PAYLOAD))
    class CountedQuery:
        calls=0
        def __call__(self,doc): self.calls+=1; return doc['active']
        def is_cacheable(self): return True
    query=CountedQuery()
    first=database.search(query)
    assert query.calls==1
    first[0]['profile']['tags'].append('bad')
    first[0].doc_id=999
    first.clear()
    second=database.search(query)
    assert query.calls==1, 'cache must still be used'
    assert second[0]==PAYLOAD and second[0].doc_id==1
    second[0]['profile']['name']='bad'
    assert database.search(query)[0]['profile']['name']=='Ada'


def test_writes_and_cache_invalidation_still_work(database):
    original=database.insert(deepcopy(PAYLOAD))
    query=Query().active==True
    assert len(database.search(query))==1
    database.update({'active':False},doc_ids=[original])
    assert database.search(query)==[]
    database.update(lambda doc: doc['profile']['tags'].append('stored'),doc_ids=[original])
    assert database.get(doc_id=original)['profile']['tags']==['safe','stored']
    added=database.insert(deepcopy(PAYLOAD))
    assert [doc.doc_id for doc in database.search(query)]==[added]
    assert database.remove(doc_ids=[added])==[added]
    assert database.search(query)==[]
    assert database.contains(doc_id=original)
    assert database.get(doc_id=999) is None
    assert database.get(doc_ids=[999])==[]


def test_per_table_overrides_and_existing_table_identity(database):
    unsafe=database.table('unsafe',copy_on_read=False)
    safe=database.table('safe')
    for table in (unsafe,safe): table.insert(deepcopy(PAYLOAD))
    query=Query().active==True
    unsafe.search(query)[0]['profile']['name']='legacy-mutation'
    assert unsafe.search(query)[0]['profile']['name']=='legacy-mutation'
    safe.search(query)[0]['profile']['name']='bad'
    assert safe.search(query)[0]['profile']['name']=='Ada'
    assert database.table('safe',copy_on_read=False) is safe
    assert database.table('unsafe') is unsafe


def test_direct_table_and_explicit_opt_in_on_default_database():
    table=Table(MemoryStorage(),'direct',copy_on_read=True)
    ident=table.insert(deepcopy(PAYLOAD))
    table.get(doc_id=ident)['profile']['tags'].append('bad')
    assert table.get(doc_id=ident)==PAYLOAD
    with TinyDB(storage=MemoryStorage) as db:
        table=db.table('safe',copy_on_read=True)
        ident=table.insert(deepcopy(PAYLOAD))
        table.get(doc_id=ident)['profile']['tags'].append('bad')
        assert table.get(doc_id=ident)==PAYLOAD

@pytest.mark.parametrize('options',[{}, {'copy_on_read':False}])
def test_default_behavior_and_legacy_table_subclasses_remain_compatible(options):
    class LegacyTable(Table):
        def __init__(self,storage,name): super().__init__(storage,name)
    class LegacyDB(TinyDB): table_class=LegacyTable
    with LegacyDB(storage=MemoryStorage,**options) as db:
        db.insert(deepcopy(PAYLOAD))
        query=Query().active==True
        first=db.search(query)
        second=db.search(query)
        assert first is not second
        assert first[0] is second[0]


def test_option_never_reaches_storage_constructor():
    class StrictStorage(MemoryStorage):
        def __init__(self,marker): super().__init__(); self.marker=marker
    with TinyDB(storage=StrictStorage,marker='sentinel',copy_on_read=True) as db:
        assert db.storage.marker=='sentinel'
        ident=db.insert(deepcopy(PAYLOAD))
        db.get(doc_id=ident)['profile']['tags'].append('bad')
        assert db.get(doc_id=ident)==PAYLOAD


def test_reading_does_not_write_storage():
    class CountWrites(MemoryStorage):
        writes=0
        def write(self,data): self.writes+=1; super().write(data)
    with TinyDB(storage=CountWrites,copy_on_read=True) as db:
        ident=db.insert(deepcopy(PAYLOAD))
        before=db.storage.writes
        for api in ('all','get-id','get-query','get-ids','iter','search'):
            read_one(db,api,ident)['profile']['tags'].append('bad')
        assert db.storage.writes==before


def test_custom_document_metadata_is_preserved():
    class CustomDocument(Document):
        def __init__(self,value,doc_id): super().__init__(value,doc_id); self.marker=['custom']
    class CustomTable(Table): document_class=CustomDocument
    class CustomDB(TinyDB): table_class=CustomTable
    with CustomDB(storage=MemoryStorage,copy_on_read=True) as db:
        ident=db.insert(deepcopy(PAYLOAD))
        query=Query().active==True
        first=db.search(query)[0]
        assert type(first) is CustomDocument and first.marker==['custom'] and first.doc_id==ident
        first.marker.append('bad')
        first['profile']['tags'].append('bad')
        again=db.search(query)[0]
        assert again.marker==['custom'] and again==PAYLOAD


def test_json_file_reopen_and_other_tables_are_unchanged(tmp_path):
    path=tmp_path/'real.json'
    with TinyDB(path,copy_on_read=True) as db:
        ident=db.insert(deepcopy(PAYLOAD))
        db.table('other').insert({'untouched':[1,2,3]})
        before=path.read_bytes()
        db.search(Query().active==True)[0]['profile']['tags'].append('bad')
        db.get(doc_id=ident)['profile']['name']='bad'
        assert path.read_bytes()==before
    with TinyDB(path,copy_on_read=True) as reopened:
        assert reopened.get(doc_id=ident)==PAYLOAD
        assert reopened.table('other').all()==[{'untouched':[1,2,3]}]
