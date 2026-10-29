"""Regressions found by actual browser and six-scene HTTP smoke."""
from finpilot.intent.compiler import IntentContext, compile_goal, blocking_clarifications
from finpilot.timeutil import utcnow


def test_wealth_comparison_keeps_explicit_budget(session,seed,ctx):
    result=compile_goal(IntentContext(session=session,auth=ctx),'比较理财产品，预算1000元',[],utcnow())
    assert result['hard_constraints']['total_budget_minor']==100000
    assert 'compare_products' in result['goals']
    assert not blocking_clarifications(result)


def test_wealth_comparison_without_budget_asks(session,seed,ctx):
    result=compile_goal(IntentContext(session=session,auth=ctx),'比较理财产品',[],utcnow())
    assert any(c['field']=='total_budget_minor' for c in blocking_clarifications(result))


def test_card_query_is_not_card_application(session,seed,ctx):
    result=compile_goal(IntentContext(session=session,auth=ctx),'查看我的银行卡',[],utcnow())
    assert 'card_status' in result['goals']
    assert 'card_apply' not in result['goals']


def test_unclear_card_intent_does_not_default_to_application(session,seed,ctx):
    result=compile_goal(IntentContext(session=session,auth=ctx),'银行卡',[],utcnow())
    assert 'card_apply' not in result['goals']
    assert blocking_clarifications(result)


def test_http_dispatch_waits_until_request_commit(api_client,seed,session,session_factory,monkeypatch):
    from sqlalchemy import select
    from finpilot.config import get_settings
    from finpilot.kernel.provider_adapter import ProviderResult,register_adapter
    from finpilot.models import Outbox
    from finpilot.workers.outbox import drain

    calls=[]
    class Provider:
        provider_id='bank_sandbox_v1'
        def capabilities(self):return {'stable_key':True,'query':True,'cancel':True}
        def submit(self,*,stable_operation_ref,payload):
            # Another transaction can only see the outbox row if the request committed.
            with session_factory() as independent:
                assert independent.scalar(select(Outbox).where(Outbox.operation_id==stable_operation_ref)) is not None
            calls.append(stable_operation_ref)
            return ProviderResult(provider_ref='provider-1',status='accepted')
        def query(self,*,stable_operation_ref):return ProviderResult(provider_ref='provider-1',status='pending')
        def cancel(self,*,stable_operation_ref):return ProviderResult(provider_ref='provider-1',status='cancelled')

    monkeypatch.setenv('PROVIDER_MODE','http');get_settings.cache_clear();register_adapter(Provider())
    try:
        login=api_client.post('/v1/auth/sessions',json={'user_id':seed['user_id'],'password':seed['password'],'channel':'app'}).json()
        headers={'Authorization':'Bearer '+login['data']['token']}
        created=api_client.post('/v1/tasks',json={'message':'给李四转100元','channel':'app'},headers=headers).json()['data']
        plan=created['plan']
        confirmed=api_client.post(f"/v1/plans/{plan['plan_id']}/confirm",headers=headers,
            json={'scope_hash':plan['scope_hash'],'expected_revision':created['revision']}).json()
        assert confirmed['data']['execution']['status']=='reconciling'
        assert calls==[]
        session.expire_all();drain(session);session.commit()
        assert len(calls)==1
    finally:get_settings.cache_clear()
