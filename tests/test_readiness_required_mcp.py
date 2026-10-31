import unittest
from types import SimpleNamespace as S
from src.open_llm_vtuber.startup_status import conversation_status


class RequiredReadinessTests(unittest.TestCase):
    def context(self, enabled=True, expected=('weather',), availability=None):
        return S(asr_engine=object(), tts_engine=object(), agent_engine=object(),
                 tool_adapter=S(availability=availability or {}),
                 _mcp_enabled=enabled, _mcp_expected_servers=expected,
                 mcp_client=object(), tool_manager=object(), tool_executor=object())

    def test_empty_availability_is_not_ready(self):
        self.assertFalse(conversation_status(self.context())['required_mcp_ready'])

    def test_disabled_differs_from_enabled_unconfirmed(self):
        disabled=conversation_status(self.context(enabled=False))
        unknown=conversation_status(self.context())
        self.assertEqual(disabled['required_mcp_state'], 'disabled')
        self.assertEqual(unknown['required_mcp_state'], 'unconfirmed')
        self.assertFalse(disabled['required_mcp_ready'])

    def test_missing_configuration_and_empty_required_fail_closed(self):
        for expected in (None, (), ('ddg-search',)):
            self.assertFalse(conversation_status(self.context(expected=expected))['required_mcp_ready'])

    def test_every_expected_required_server_must_be_present(self):
        c=self.context(expected=('weather','komugi-context'), availability={'weather':{'state':'ready','required':True}})
        self.assertFalse(conversation_status(c)['required_mcp_ready'])

    def test_missing_fields_and_uninitialized_components_fail_closed(self):
        for value in ({}, {'state':'ready'}, {'required':True}, {'state':'ready','required':False}):
            self.assertFalse(conversation_status(self.context(availability={'weather':value}))['required_mcp_ready'])
        c=self.context(availability={'weather':{'state':'ready','required':True}})
        c.tool_executor=None
        self.assertFalse(conversation_status(c)['required_mcp_ready'])

    def test_ready_requires_complete_required_evidence_optional_can_fail(self):
        c=self.context(expected=('weather','ddg-search'), availability={
            'weather':{'state':'ready','required':True}, 'ddg-search':{'state':'timeout','required':False}})
        self.assertTrue(conversation_status(c)['required_mcp_ready'])
        self.assertEqual(conversation_status(c)['required_mcp_state'], 'ready')
