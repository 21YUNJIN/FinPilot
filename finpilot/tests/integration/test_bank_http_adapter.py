"""Independent bank sandbox contract and ledger reconciliation regressions."""
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import bank
from finpilot.adapters.bank_http import HttpBankAdapter
from finpilot.config import get_settings
from finpilot.errors import FinPilotError
from finpilot.kernel.grants import create_exact_grant
from finpilot.kernel.provider_adapter import register_adapter
from finpilot.kernel.submission import OperationDraft, prepare_operation, submit_operation
from finpilot.models import Account, LedgerTransaction
from finpilot.security.scope import build_scope
from finpilot.timeutil import utcnow
from finpilot.workers.outbox import drain
from finpilot.workers.reconciliation import reconcile_unknown_operations


@pytest.fixture()
def sandbox(tmp_path):
    with TestClient(bank.create_app(state_path=tmp_path/'bank.sqlite3')) as client:
        yield client


def draft():
    return {'tool':'transfer.submit','owner_user_id':'user_demo_01','typed_args':{
        'payee_id':'payee_lisi','source_account_id':'account_salary','amount_minor':10000,'fee_minor':0}}


def test_stable_key_submission_and_query(sandbox):
    adapter=HttpBankAdapter(client=sandbox)
    first=adapter.submit(stable_operation_ref='test-stable-ref',payload=draft())
    repeated=adapter.submit(stable_operation_ref='test-stable-ref',payload=draft())
    assert first.provider_ref==repeated.provider_ref and first.status=='accepted'
    advanced=sandbox.post('/bank/advance',json={'stable_operation_ref':'test-stable-ref','settle':True})
    assert advanced.status_code==200
    result=adapter.query(stable_operation_ref='test-stable-ref')
    assert result.status=='succeeded' and result.events[0]['operation_id']=='test-stable-ref'
    changed=draft();changed['typed_args']['amount_minor']=20000
    with pytest.raises(FinPilotError) as err:
        adapter.submit(stable_operation_ref='test-stable-ref',payload=changed)
    assert err.value.code=='IDEMPOTENCY_CONFLICT'


def test_timeout_is_unknown_not_failed():
    def handler(request):
        raise httpx.ReadTimeout('response lost', request=request)
    with httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FinPilotError) as err:
            HttpBankAdapter(client=client).submit(stable_operation_ref='timeout-test',payload=draft())
    assert err.value.code=='PROVIDER_TIMEOUT'


def test_server_error_after_submit_is_unknown():
    def handler(request):
        return httpx.Response(503,json={"error":{"code":"SERVER_ERROR"}},request=request)
    with httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(FinPilotError) as err:
            HttpBankAdapter(client=client).submit(stable_operation_ref='server-error-test',payload=draft())
    assert err.value.code=='PROVIDER_TIMEOUT'


def test_remote_provider_settles_once_via_bank_ledger(session,seed,ctx,sandbox,monkeypatch):
    monkeypatch.setenv('PROVIDER_MODE','http');get_settings.cache_clear()
    adapter=HttpBankAdapter(client=sandbox);register_adapter(adapter)
    try:
        operation=prepare_operation(session,ctx,OperationDraft(tool='transfer.submit',
            typed_args=draft()['typed_args'],operation_id='http-transfer-test',
            source_account_id='account_salary',amount_minor=10000))
        scope=build_scope(operation.tool,operation.typed_args,task_id=operation.task_id,
            plan_id=operation.plan_id,plan_revision=operation.plan_revision,
            parameters_hash=operation.parameters_hash,operation_id=operation.operation_id,
            required_level=operation.required_level)
        grant=create_exact_grant(session,ctx,plan_id=operation.plan_id,plan_revision=operation.plan_revision,
                                 scope=scope,required_level='YELLOW')
        submit_operation(session,ctx,operation_id=operation.operation_id,grant_id=grant.grant_id)
        session.commit();drain(session);session.commit();session.refresh(operation)
        assert operation.provider_id=='bank_sandbox_v1' and operation.status=='pending'
        assert session.get(Account,'account_salary').posted_balance_minor==900000
        response=sandbox.post('/bank/advance',json={'stable_operation_ref':operation.operation_id,'settle':True})
        assert response.status_code==200
        reconcile_unknown_operations(session,now=utcnow()+timedelta(seconds=5));session.commit()
        session.refresh(operation)
        assert operation.status=='succeeded'
        assert session.get(Account,'account_salary').posted_balance_minor==890000
        count=len(session.scalars(select(LedgerTransaction).where(LedgerTransaction.operation_id==operation.operation_id)).all())
        reconcile_unknown_operations(session,now=utcnow()+timedelta(seconds=10));session.commit()
        assert session.get(Account,'account_salary').posted_balance_minor==890000
        assert len(session.scalars(select(LedgerTransaction).where(LedgerTransaction.operation_id==operation.operation_id)).all())==count
    finally:get_settings.cache_clear()
