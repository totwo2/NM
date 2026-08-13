"""
tests/test_task_router.py — TaskRouter 路由规则测试
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from nm.task_router import TaskRouter, TaskSignal
from nm.types import Complexity, Domain


class FakeMM:
    def __init__(self):
        self._user_groups = {}
    def get_model(self, user_id, model_name=None):
        return None, "fake-model"
    def assign_user_to_group(self, user_id, group_id):
        pass


def make_router():
    return TaskRouter(
        FakeMM(),
        local_model="local-model",
        cloud_model="cloud-model",
        cloud_strong_model="cloud-strong",
    )


def signal(complexity=Complexity.MEDIUM, domain=Domain.GENERAL,
           has_pii=False, needs_tools=False, needs_reasoning=False):
    return TaskSignal(
        complexity=complexity, domain=domain,
        has_pii=has_pii, estimated_tokens=100,
        needs_tools=needs_tools, needs_reasoning=needs_reasoning,
        confidence=0.5,
    )


class TestDefaultRules:
    def test_pii_forces_local(self):
        router = make_router()
        sig = signal(has_pii=True)
        for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES:
            decision = rule_fn(sig, router)
            if decision is not None:
                assert decision.provider == "local"
                assert decision.model_name == "local-model"
                return
        pytest.fail("PII rule did not match")

    def test_complex_reasoning_uses_strong_cloud(self):
        router = make_router()
        sig = signal(complexity=Complexity.COMPLEX, needs_reasoning=True)
        for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES:
            decision = rule_fn(sig, router)
            if decision is not None:
                assert decision.provider == "cloud"
                assert decision.model_name == "cloud-strong"
                return
        pytest.fail("complex+reasoning rule did not match")

    def test_complex_uses_cloud(self):
        router = make_router()
        sig = signal(complexity=Complexity.COMPLEX, needs_reasoning=False)
        for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES:
            decision = rule_fn(sig, router)
            if decision is not None:
                assert decision.provider == "cloud"
                assert decision.model_name == "cloud-model"
                return
        pytest.fail("complex rule did not match")

    def test_code_uses_cloud(self):
        router = make_router()
        sig = signal(domain=Domain.CODE)
        for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES:
            decision = rule_fn(sig, router)
            if decision is not None:
                assert decision.provider == "cloud"
                return
        pytest.fail("code rule did not match")

    def test_document_uses_local(self):
        router = make_router()
        sig = signal(domain=Domain.DOCUMENT)
        for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES:
            decision = rule_fn(sig, router)
            if decision is not None:
                assert decision.provider == "local"
                return
        pytest.fail("document rule did not match")

    def test_default_general_does_not_match(self):
        router = make_router()
        sig = signal()
        matched = any(rule_fn(sig, router) is not None for rule_fn in TaskRouter.DEFAULT_ROUTING_RULES)
        assert not matched, "GENERAL/MEDIUM should not match any specific rule"

    def test_rule_priority_pii_first(self):
        """PII rule is first and should match before complex+reasoning"""
        router = make_router()
        sig = signal(complexity=Complexity.COMPLEX, needs_reasoning=True, has_pii=True)
        first_decision = TaskRouter.DEFAULT_ROUTING_RULES[0](sig, router)
        assert first_decision is not None
        assert first_decision.provider == "local"


class TestConfigurableRules:
    def test_custom_rules_override(self):
        router = make_router()

        def always_local(sig, r):
            return type('D', (), {'provider': 'local', 'model_name': 'forced-local',
                                   'fallback_model': 'cloud-model', 'reason': 'custom'})()

        router.routing_rules = [always_local]
        rules = getattr(router, "routing_rules", TaskRouter.DEFAULT_ROUTING_RULES)
        assert rules == [always_local]

    def test_default_rules_count(self):
        assert len(TaskRouter.DEFAULT_ROUTING_RULES) == 6
