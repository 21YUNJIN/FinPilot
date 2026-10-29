"""Real HTTP integration with a separate StockQuant process and real PostgreSQL."""
import json
import os
import socket
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import requests
from sqlalchemy import select, func

from finpilot.config import get_settings
from finpilot.models import WealthProduct, ProductNav, GrantUse, Operation
from finpilot.adapters.stockquant.remote import fetch_reports, history_view
from finpilot.adapters.stockquant.http_client import StockQuantRiskClient, QuantContractError
from finpilot.domains.wealth_rules.compare import compare_products

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope='module')
def quant_http(tmp_path_factory):
    python = os.environ.get('FINPILOT_QUANT_PYTHON')
    if not python:
        pytest.fail('Set FINPILOT_QUANT_PYTHON to the independent S3 Python environment.')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    token = uuid4().hex
    tmp = tmp_path_factory.mktemp('real-quant')
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT/'vendor/stockquant'), SQ_BANK_TOKEN=token,
               SQ_BANK_AUDIT_PATH=str(tmp/'audit.jsonl'), PYTHONUTF8='1')
    with (tmp/'server.log').open('w',encoding='utf-8') as log:
        proc = subprocess.Popen([python, '-m', 'stockquant_bank', 'serve', '--port', str(port)],
            cwd=ROOT/'services/quant-service', env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        url = f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                if proc.poll() is not None: pytest.fail((tmp/'server.log').read_text(encoding='utf-8'))
                try:
                    if requests.get(url+'/healthz', timeout=.25).ok: break
                except requests.RequestException: pass
                time.sleep(.1)
            else: pytest.fail('quant HTTP did not become healthy')
            yield url, token
        finally:
            proc.terminate(); proc.wait(timeout=10)


@pytest.fixture()
def remote_config(monkeypatch, quant_http):
    url, token = quant_http
    monkeypatch.setenv('QUANT_BACKEND', 'remote')
    monkeypatch.setenv('STOCKQUANT_SERVICE_URL', url)
    monkeypatch.setenv('STOCKQUANT_SERVICE_TOKEN', token)
    get_settings.cache_clear()
    yield url, token
    get_settings.cache_clear()


def product(session, pid='remote-product', count=300, kind='fixed_income', gap=1):
    p = WealthProduct(product_id=pid,name='合成净值产品',issuer='测试机构',product_type=kind,
        terms_version='2026.10',issuer_risk_level='R2',min_minor=100,fee_bps=0,fee_fixed_minor=0,
        term_days=0,open_days=[0,1,2,3,4],cutoff_time='15:00',liquidity_terms='测试条款',
        cancel_terms='',cooling_off_days=0,settlement_days=1,history_ref='synthetic-test',meta={},is_active=True)
    session.add(p);session.flush()
    value = 1_000_000
    for i in range(count):
        value = round(value * (1.001 if i%3 else .998))
        session.add(ProductNav(nav_id=f'{pid}-{i}', product_id=pid, as_of=date(2025,1,1)+timedelta(days=i*gap),
            nav_e6=value, daily_return_bps=999, terms_version='2026.10'))
    session.flush()
    return p


def test_real_http_uses_nav_and_converts_units(session, seed, ctx, remote_config):
    p=product(session)
    report=fetch_reports(session,[p],date(2026,10,22))[p.product_id]
    assert report['source_verified'] is True
    assert report['engine']['name']=='stockquant'
    assert report['metrics']['annual_volatility']['value']>0
    navs=list(session.scalars(select(ProductNav).where(ProductNav.product_id==p.product_id).order_by(ProductNav.as_of)))
    history=history_view(report,navs)
    assert history['history_metric']['value']==pytest.approx(report['metrics']['cagr']['value']*10000,abs=.0001)
    # bogus daily_return_bps=999 must not be used by the remote NAV pipeline.
    assert abs(report['metrics']['cagr']['value'])<.1
    items=compare_products(session,ctx,budget_minor=100000,horizon_days=365,preferences=['yield'])
    assert items[0]['quant_risk']['source_verified']


def test_non_nav_never_gets_stock_risk(session, seed, remote_config):
    p=product(session,kind='deposit')
    assert fetch_reports(session,[p],date(2026,10,22))[p.product_id]['reason_code']=='NON_NAV_PRODUCT'


def test_weekly_data_is_not_annualized_as_daily(session,seed,remote_config):
    p=product(session,count=20,gap=7)
    assert fetch_reports(session,[p],date(2026,10,22))[p.product_id]['reason_code']=='NON_DAILY_OR_GAPPED_NAV'


def test_missing_history_is_na(session,seed,remote_config):
    p=product(session,count=0)
    report=fetch_reports(session,[p],date(2026,10,22))[p.product_id]
    assert report['status']=='unavailable'
    assert history_view(report,[])['history_metric']['value'] is None


def test_service_failure_does_not_fall_back(session,seed,ctx,remote_config,monkeypatch):
    product(session)
    monkeypatch.setenv('STOCKQUANT_SERVICE_URL','http://127.0.0.1:1')
    get_settings.cache_clear()
    items=compare_products(session,ctx,budget_minor=100000,horizon_days=365,preferences=['yield'])
    assert items[0]['history']['history_metric']['value'] is None
    assert items[0]['quant_risk']['status']=='unavailable'


@pytest.mark.parametrize('tamper',['hash','missing','unit','invalid_product','invalid_provenance','invalid_metric'])
def test_tampered_service_data_is_rejected(remote_config,tamper):
    url,token=remote_config
    class TamperSession(requests.Session):
        def post(self,*a,**kw):
            r=super().post(*a,**kw);body=r.json()
            if tamper=='hash':body['data']['products'][0]['provenance']['source_hash']='wrong'
            if tamper=='missing':body['data']['products']=[]
            if tamper=='unit':body['data']['products'][0]['metrics']['annual_volatility']['unit']='bps'
            if tamper=='invalid_product':body['data']['products']=['not-an-object']
            if tamper=='invalid_provenance':body['data']['products'][0]['provenance']='not-an-object'
            if tamper=='invalid_metric':body['data']['products'][0]['metrics']['annual_volatility']='not-an-object'
            r._content=json.dumps(body).encode();return r
    nav=[{'date':'2025-01-01','value':1.0},{'date':'2025-01-02','value':1.01}]
    with TamperSession() as s:
        with pytest.raises(QuantContractError):
            StockQuantRiskClient(url,token,session=s).risk_report([{'product_id':'p','nav':nav}],as_of='2026-10-22')


def test_request_ids_do_not_depend_on_milliseconds(remote_config,monkeypatch):
    url,token=remote_config
    monkeypatch.setattr('finpilot.adapters.stockquant.http_client.time.time',lambda:1234.0)
    client=StockQuantRiskClient(url,token)
    p=[{'product_id':'p','nav':[{'date':'2025-01-01','value':1.0}]}]
    try:
        a=client.risk_report(p,as_of='2026-10-22');b=client.risk_report(p,as_of='2026-10-22')
        assert a.request_id != b.request_id
    finally:client.session.close()


def login(client,seed):
    r=client.post('/v1/auth/sessions',json={'user_id':seed['user_id'],'password':seed['password'],'channel':'app'})
    assert r.status_code==200
    return {'Authorization':'Bearer '+r.json()['data']['token']}


def test_subscription_query_requires_human_confirmation(api_client,seed,session):
    headers=login(api_client,seed)
    before=api_client.get('/v1/subscriptions',headers=headers).json()['data']
    assert before['items']==[] and before['requires_confirmation'] is True
    assert api_client.post('/v1/subscriptions/query',headers=headers,json={'confirmed':False,'operation_id':'query-test-001'}).status_code==422
    r=api_client.post('/v1/subscriptions/query',headers=headers,json={'confirmed':True,'operation_id':'query-test-001'})
    assert r.status_code==200,r.text
    assert r.json()['data']['requires_confirmation'] is False
    uses=session.scalar(select(func.count()).select_from(GrantUse))
    assert uses==1
    assert api_client.post('/v1/subscriptions/query',headers=headers,json={'confirmed':True,'operation_id':'query-test-001'}).status_code==200
    assert session.scalar(select(func.count()).select_from(GrantUse))==uses
    assert session.get(Operation,'query-test-001').required_level=='YELLOW'


@pytest.mark.parametrize('mode,expected',[('live','DEPENDENCY_FAILED'),('offline_replay','CAPABILITY_NOT_SUPPORTED')])
def test_unconfigured_live_and_unimplemented_replay_fail_closed(api_client,seed,monkeypatch,mode,expected):
    headers=login(api_client,seed)
    monkeypatch.setenv('APP_MODE',mode)
    monkeypatch.setenv('MODEL_BASE_URL','')
    monkeypatch.setenv('MODEL_API_KEY','')
    monkeypatch.setenv('MODEL_NAME','')
    get_settings.cache_clear()
    try:
        r=api_client.post('/v1/tasks',headers=headers,json={'message':'查询余额','channel':'app'})
        assert r.json()['status'] != 'ok'
        assert r.json()['error']['code']==expected
    finally:get_settings.cache_clear()
