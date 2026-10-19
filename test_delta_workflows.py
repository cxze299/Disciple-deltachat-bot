import logging
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import menxun_bot as core
from delta_workflows import DeltaWorkflows


class DeltaWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.site = core.SiteConfig('cedar-test', '门训 / 灵命组', 'https://example.test/api/bot/groups/test', frozenset())
        self.bot = SimpleNamespace(rpc=Mock(), logger=logging.getLogger('workflow-test'))
        self.flow = DeltaWorkflows(core, self.bot, 2)
        self.state = {'delta_chat_types': {'101': 2}, 'delta_chat_names': {'101': '个人灵命组'}}

    def test_group_binding_requires_confirmation_and_keeps_delta_account(self):
        group = SimpleNamespace(chat_type=core.ChatType.GROUP, self_in_group=True)
        self.bot.rpc.get_full_chat_by_id.return_value = group
        with patch.object(core, 'state', self.state), patch.object(core, 'SITES', (self.site,)), patch.object(core, 'SITE_BY_ID', {self.site.site_id: self.site}), patch.object(core, 'SITE_BY_CHAT_ID', {}), patch.object(core, 'is_super_admin', return_value=True), patch.object(core, 'send') as send, patch.object(core, 'bind_group_to_site') as bind:
            self.flow._handle_group_binding_flow(900, 77, '管理员 绑定群')
            self.flow._handle_group_binding_flow(900, 77, '1')
            bind.assert_not_called()
            self.assertIn('回复“确认”', send.call_args.args[3])
            self.flow._handle_group_binding_flow(900, 77, '确认')
            self.bot.rpc.get_full_chat_by_id.assert_called_once_with(2, 101)
            bind.assert_called_once_with(self.site, 101)

    def test_direct_legacy_binding_command_still_reaches_core(self):
        self.assertFalse(self.flow._handle_group_binding_flow(900, 77, '管理员 绑定群 科大 11'))

    def test_non_admin_cannot_start_binding(self):
        with patch.object(core, 'is_super_admin', return_value=False), patch.object(core, 'send') as send:
            self.flow._handle_group_binding_flow(900, 77, '管理员 绑定群')
            self.assertIn('仅限超级管理员', send.call_args.args[3])
            self.assertFalse(self.flow.group_binding_flows)

    def test_history_summary_does_not_replace_global_snapshot(self):
        snapshot = ({'members': ['甲'], 'records': [{'name': '甲', 'logical_date': '2026-04-06', 'daily': 'done'}]}, {})
        with patch.object(core, 'website_snapshot', return_value=snapshot) as fetch, patch.object(core, 'now', return_value=datetime(2026, 4, 7)):
            result = self.flow._history_group_summary(self.site)
            self.assertIn('【总体】1/2 项', result)
            self.assertIn('甲', result)
            self.assertIs(core.website_snapshot, fetch)
            fetch.assert_called_once_with(self.site)

    def test_delta_group_discovery_ignores_private_chats(self):
        self.bot.rpc.get_chatlist_entries.return_value = [101, 900]
        self.bot.rpc.get_full_chat_by_id.side_effect = [SimpleNamespace(chat_type=core.ChatType.GROUP, self_in_group=True, name='灵命组'), SimpleNamespace(chat_type='Single', self_in_group=False)]
        with patch.object(core, 'state', {}), patch.object(core, 'save_state'):
            self.flow.discover_groups()
            self.assertEqual(core.state['delta_chat_types'], {'101': 2})


if __name__ == '__main__':
    unittest.main()
