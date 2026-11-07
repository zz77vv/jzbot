"""Offline safety gates for V2's own publisher, never touching GitHub."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import publish as p


def fixture_source(root):
    files={'jzbot/__init__.py':'"""fixture"""\n','jzbot/app.py':'value=1\n',
           'pyproject.toml':'[project]\nname="fixture"\n','README.md':'fixture\n','AGENTS.md':'fixture\n'}
    for name,text in files.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    return files


@pytest.mark.parametrize('name',['ledger.db','ledger.db-wal','ledger.db-shm','bot.log','run.log',
                                  '.env','.env.prod','test.session','.git/config','.github-publish/jzbot/app.py',
                                  '../jzbot/app.py','jzbot/__pycache__/secret.py','reports/customer.json',
                                  'tests/fixture.db','scripts/credentials.json',
                                  'broadcast.db','broadcast.db-wal','broadcast.db-shm'])
def test_runtime_sensitive_files_never_allowed(name):
    assert not p.allowed(name)


@pytest.mark.parametrize('name',['jzbot/config.py','scripts/check.py','tests/test_amounts.py',
                                  'docs/ARCHITECTURE.md','README.md','requirements-dev.txt'])
def test_only_source_and_necessary_documents_allowed(name):
    assert p.allowed(name)


def test_collect_ignores_databases_credentials_caches_and_other_projects(tmp_path):
    expected=fixture_source(tmp_path)
    for name in ('ledger.db','.env','runtime/config.json','reports/customers.json',
                 '.github-publish/unrelated.py','jzbot/__pycache__/x.py'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private-data',encoding='utf-8')
    assert p.collect(tmp_path)==expected


@pytest.mark.parametrize('kind',['telegram','github','literal-password','private-key'])
def test_secret_scan_blocks_credentials_without_echoing_value(kind):
    if kind=='telegram': secret=str(987654321)+":"+"Z"*35; source='x='+repr(secret)
    elif kind=='github': secret='gh'+'p_'+'a'*40;source='x='+repr(secret)
    elif kind=='literal-password': secret='synthetic-'+'password-'+'value';source='PASSWORD='+repr(secret)
    else: secret='-----BEGIN '+'PRIVATE KEY-----';source='x='+repr(secret)
    with pytest.raises(p.PublishError) as err: p.scan(source,'fixture.py')
    assert secret not in str(err.value)


@pytest.mark.parametrize('problem',['wrong-user','wrong-owner','public','read-only','branch','wrong-id','missing-id'])
def test_wrong_repository_identity_or_privacy_stops(monkeypatch,problem):
    user={'login':'zz77vv'}
    repo={'id':1434510965,'full_name':p.REPOSITORY,'owner':{'login':'zz77vv'},'private':True,
          'permissions':{'push':True},'default_branch':'main'}
    if problem=='wrong-user':user['login']='other'
    if problem=='wrong-owner':repo['owner']['login']='other'
    if problem=='public':repo['private']=False
    if problem=='read-only':repo['permissions']['push']=False
    if problem=='branch':repo['default_branch']='unrelated'
    if problem=='wrong-id':repo['id']=1434510966
    if problem=='missing-id':repo.pop('id')
    monkeypatch.setattr(p,'api',lambda endpoint:user if endpoint=='user' else repo)
    with pytest.raises(p.PublishError):p.verify_repository()


def test_local_test_failure_prevents_creation_and_push(tmp_path,monkeypatch):
    fixture_source(tmp_path);network=Mock()
    monkeypatch.setattr(p,'ROOT',tmp_path)
    monkeypatch.setattr(p,'validate',Mock(side_effect=RuntimeError('test fixture failure')))
    monkeypatch.setattr(p,'run',network)
    with pytest.raises(RuntimeError,match='test fixture failure'):p.publish(create_private=True)
    network.assert_not_called()


def test_create_flag_refuses_replacement_repository_without_creation_or_push(tmp_path,monkeypatch):
    fixture_source(tmp_path)
    network=Mock(side_effect=AssertionError('Repository creation must not be attempted'))
    monkeypatch.setattr(p,'ROOT',tmp_path)
    monkeypatch.setattr(p,'validate',Mock())
    monkeypatch.setattr(p,'api',lambda endpoint:{'login':'zz77vv'})
    monkeypatch.setattr(p,'run',network)
    with pytest.raises(p.PublishError,match='existing repository'):
        p.publish(create_private=True)
    network.assert_not_called()


def test_source_change_during_tests_stops_before_github(tmp_path,monkeypatch):
    fixture_source(tmp_path);network=Mock()
    monkeypatch.setattr(p,'ROOT',tmp_path)
    monkeypatch.setattr(p,'validate',lambda root:(root/'jzbot/app.py').write_text('value=2\n',encoding='utf-8'))
    monkeypatch.setattr(p,'run',network)
    with pytest.raises(p.PublishError,match='Source changed'):p.publish(create_private=True)
    network.assert_not_called()


def test_index_unexpected_file_stops(monkeypatch):
    monkeypatch.setattr(p,'git',Mock(return_value='ledger.db\0'))
    with pytest.raises(p.PublishError,match='Index'):p.audit_index({'jzbot/app.py':'value=1\n'})


def test_deleted_secret_diff_is_still_blocked(monkeypatch):
    secret=str(987654321)+":"+"Z"*35
    git=Mock(side_effect=['jzbot/app.py\0','value=1\n','', '-secret='+secret])
    monkeypatch.setattr(p,'git',git)
    with pytest.raises(p.PublishError):p.audit_index({'jzbot/app.py':'value=1\n'})


def test_historical_runtime_file_or_secret_is_blocked(monkeypatch):
    monkeypatch.setattr(p,'git',Mock(side_effect=['fixture-sha','ledger.db']))
    with pytest.raises(p.PublishError,match='historical file'):p.audit_history()
    secret=str(987654321)+":"+"Z"*35
    monkeypatch.setattr(p,'git',Mock(side_effect=['fixture-sha','jzbot/app.py',repr(secret)]))
    with pytest.raises(p.PublishError):p.audit_history()


def test_publisher_accepts_canonical_repository_and_remote(monkeypatch):
    user={'login':'zz77vv'}
    repo={'id':1434510965,'full_name':'zz77vv/jzbot','owner':{'login':'zz77vv'},
          'private':True,'permissions':{'push':True},'default_branch':'main'}
    def api(endpoint):
        if endpoint=='user':return user
        assert endpoint=='repos/zz77vv/jzbot'
        return repo
    monkeypatch.setattr(p,'api',api)
    assert p.verify_repository()==user
    assert p.REMOTE=='https://github.com/zz77vv/jzbot.git' and p.BRANCH=='main'
    assert p.MIRROR.parent==p.ROOT

