from types import SimpleNamespace
import pytest
from helpdesk.access import AccessDenied, AuthorizedStore
from helpdesk.store import Store, combine_snapshots


@pytest.mark.asyncio
async def test_scope_removes_invisible_metadata_and_splits_cache(monkeypatch):
    bound=combine_snapshots([
        {'kb_id':'a','id':'sa','version_ids':['public','private'],'name':'visible','organization':'A'},
        {'kb_id':'b','id':'sb','version_ids':['secret'],'name':'hidden-name','organization':'B'}])
    async def snapshot(self,ids):return bound
    async def rows(sql,args=()):return [{'revision':7}]
    monkeypatch.setattr(Store,'scope_snapshot',snapshot)
    limited=AuthorizedStore(SimpleNamespace(),'user',7)
    limited.rows=rows;limited.kbs={'a'};limited.versions={'public'}
    result=await limited.scope_snapshot([])
    assert len(result['members'])==1 and result['version_ids']==['public']
    assert 'hidden-name' not in str(result)
    full=AuthorizedStore(SimpleNamespace(),'member',7)
    full.rows=rows;full.kbs={'a'};full.versions={'public','private'}
    other=await full.scope_snapshot([])
    assert result['members'][0]['id']!=other['members'][0]['id']
    with pytest.raises(AccessDenied):await limited.scope_snapshot(['b'])


@pytest.mark.asyncio
async def test_vector_and_corpus_are_filtered_before_access(monkeypatch):
    observed=[]
    async def corpus(self,snapshot):observed.append(snapshot['version_ids']);return []
    async def vector(self,snapshot,embedding,limit):observed.append(snapshot['version_ids']);return []
    monkeypatch.setattr(Store,'corpus',corpus);monkeypatch.setattr(Store,'vector_search',vector)
    authorized=AuthorizedStore(SimpleNamespace(),'u',3)
    async def rows(sql,args=()):return [{'revision':3}]
    authorized.rows=rows;authorized.versions={'allowed'}
    snapshot={'id':'frozen','version_ids':['allowed','denied']}
    await authorized.corpus(snapshot);await authorized.vector_search(snapshot,[1,0])
    assert observed==[['allowed'],['allowed']]


@pytest.mark.asyncio
async def test_revoked_revision_and_disabled_principal_fail_closed():
    authorized=AuthorizedStore(SimpleNamespace(),'u',3)
    async def changed(sql,args=()):return [{'revision':4}]
    authorized.rows=changed
    with pytest.raises(AccessDenied):await authorized.corpus({'id':'s','version_ids':[]})
    async def disabled(sql,args=()):return []
    authorized.rows=disabled
    with pytest.raises(AccessDenied):await authorized.bind()
