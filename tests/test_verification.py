import importlib.util
from pathlib import Path


def module():
    spec=importlib.util.spec_from_file_location('run_verification',Path('scripts/run_verification.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def test_percentiles_use_nearest_rank_and_keep_negative_overhead():
    values=[-10]*94+[1,2,3,4,5,6]
    stats=module().distribution(values)
    assert stats['n']==100 and stats['p95_ms']==1 and stats['p99_ms']==5 and stats['min_ms']==-10
    assert module().distribution([]) is None


def test_harness_ignores_inherited_credentials_and_uses_disposable_storage(monkeypatch):
    from verification.harness import Stack
    monkeypatch.setenv('GREENROUTE_BASELINE_ENDPOINT','unverified-provider')
    monkeypatch.setenv('GREENROUTE_GATEWAY_KEY','inherited-secret')
    stack=Stack()
    try:
        assert stack.env['GREENROUTE_GATEWAY_KEY']!='inherited-secret'
        assert 'GREENROUTE_BASELINE_ENDPOINT' not in stack.env
        assert stack.database.parent==stack.directory
        assert stack.env['GREENROUTE_MOCK_DELAY_SECONDS']=='0.1'
    finally:stack.__exit__(None,None,None)
