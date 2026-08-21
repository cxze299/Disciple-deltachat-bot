import json
import os
import tempfile
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

import menxun_bot as bot
import healthcheck
from admin_key import create_key_record, load_key_record, save_key_record, verify_key


class FakeResponse:
    status = 200

    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class MenxunBotTests(unittest.TestCase):
    def setUp(self):
        self.site = bot.SiteConfig(
            site_id="test",
            name="测试网站",
            url="https://example.test",
            chat_ids=frozenset({101}),
        )
        self.schedule = [{
            "start": "2026-08-17",
            "end": "2026-08-23",
            "title": "本周读物",
            "video": "本周视频",
            "verse": "约翰福音 3:16",
        }]

    def test_daily_uses_exact_logical_date(self):
        record = {"name": "张迦勒", "logical_date": "2026-08-20", "daily": "done"}
        self.assertTrue(bot.website_record_matches(record, "张迦勒", "每日灵修", date(2026, 8, 20), self.schedule, self.site))
        self.assertFalse(bot.website_record_matches(record, "张迦勒", "每日灵修", date(2026, 8, 21), self.schedule, self.site))

    def test_weekly_status_matches_task_identity_across_dates(self):
        record = {"name": "张迦勒", "logical_date": "2026-08-18", "book": "done"}
        self.assertTrue(bot.website_record_matches(record, "张迦勒", "周读物", date(2026, 8, 21), self.schedule, self.site))

    def test_empty_weekly_task_does_not_match(self):
        schedule = [{"start": "2026-08-17", "end": "2026-08-23", "title": "", "video": "", "verse": ""}]
        record = {"name": "张迦勒", "logical_date": "2026-08-18", "video": "done"}
        self.assertFalse(bot.website_record_matches(record, "张迦勒", "周视频", date(2026, 8, 21), schedule, self.site))

    def test_read_request_retries_transient_tls_failure(self):
        with patch.object(bot, "urlopen", side_effect=[URLError("TLS EOF"), FakeResponse(b'{"ok": true}')]), patch.object(bot.time, "sleep"):
            self.assertEqual(bot.fetch_json(self.site, "/api/state"), {"ok": True})

    def test_existing_website_status_prevents_duplicate_checkin(self):
        state = {
            "records": [{"Id": 7, "name": "张迦勒", "logical_date": "2026-08-18", "book": "done"}],
            "weeklySchedule": self.schedule,
        }
        with patch.object(bot, "website_snapshot", return_value=(state, {"weekly_schedule": self.schedule})), patch.object(bot, "post_json") as post:
            ok, _ = bot.website_checkin(self.site, "张迦勒", "周读物", logical_date="2026-08-21")
        self.assertFalse(ok)
        post.assert_not_called()

    def test_loads_multiple_sites_and_rejects_duplicate_chat_routes(self):
        rows = {"sites": [
            {"id": "one", "name": "一组", "url": "https://one.example", "chat_ids": [11]},
            {"id": "two", "name": "二组", "url": "https://two.example", "chat_ids": [22, 23]},
        ]}
        with patch.dict(os.environ, {"MENXUN_SITES_JSON": json.dumps(rows)}):
            sites = bot.load_sites()
        self.assertEqual([site.site_id for site in sites], ["one", "two"])
        self.assertEqual(sites[1].chat_ids, frozenset({22, 23}))

        rows["sites"][1]["chat_ids"] = [11]
        with patch.dict(os.environ, {"MENXUN_SITES_JSON": json.dumps(rows)}):
            with self.assertRaisesRegex(RuntimeError, "群聊 11"):
                bot.load_sites()

    def test_legacy_binding_maps_to_first_site(self):
        temporary_state = {"bindings": {"99": "旧成员"}, "active_sites": {}, "checkins": {}, "reminded": {}}
        with patch.object(bot, "state", temporary_state):
            self.assertEqual(bot.bound_name(99, bot.DEFAULT_SITE), "旧成员")

    def test_reminder_has_restart_grace_window(self):
        current = bot.datetime(2026, 8, 21, 8, 36)
        self.assertTrue(bot.reminder_due(current, "08:30"))
        self.assertFalse(bot.reminder_due(bot.datetime(2026, 8, 21, 8, 40), "08:30"))

    def test_group_chat_routes_to_its_own_site(self):
        fake_bot = SimpleNamespace(rpc=SimpleNamespace(
            get_basic_chat_info=lambda _accid, _chat_id: SimpleNamespace(chat_type=bot.ChatType.GROUP)
        ))
        with patch.object(bot, "SITE_BY_CHAT_ID", {101: self.site}):
            is_group, selected = bot.resolve_message_site(fake_bot, 1, 101, 99)
        self.assertTrue(is_group)
        self.assertEqual(selected, self.site)

    def test_timeline_is_chronological_and_deduplicated(self):
        records = [
            {"name": "乙", "logical_date": "2026-08-21", "daily": "done", "checkin_time": "2026-08-21T09:30:00+08:00"},
            {"name": "甲", "logical_date": "2026-08-21", "daily": "done", "checkin_time": "2026-08-21T08:10:00+08:00"},
            {"name": "甲", "logical_date": "2026-08-21", "daily": "done", "checkin_time": "2026-08-21T10:00:00+08:00"},
        ]
        with patch.object(bot, "website_snapshot", return_value=({"records": records}, {"weekly_schedule": self.schedule})):
            title, rows = bot.checkin_timeline(self.site, "每日灵修", "2026-08-21")
        self.assertEqual(title, "2026-08-21 · 灵修（按时间）")
        self.assertEqual(rows, ["1. 08:10  甲", "2. 09:30  乙"])

    def test_private_checkin_notifies_groups_and_confirms_privately(self):
        fake_bot = SimpleNamespace()
        with patch.object(bot, "broadcast_group_update", return_value=2) as broadcast, patch.object(bot, "send") as send:
            bot.announce_change(fake_bot, 1, 900, False, self.site, "群通知", "打卡成功")
        broadcast.assert_called_once_with(fake_bot, 1, self.site, "群通知")
        send.assert_called_once_with(fake_bot, 1, 900, "打卡成功\n已通知 2 个群。")

    def test_help_keeps_status_commands_explicit(self):
        other_site = bot.SiteConfig(site_id="other", name="其他网站", url="https://other.test", chat_ids=frozenset())
        with patch.object(bot, "SITES", (self.site, other_site)), patch.object(bot, "DEFAULT_SITE", self.site):
            text = bot.help_text(self.site)
        self.assertIn("首次使用（请私聊）", text)
        self.assertIn("网站 — 查看可用网站", text)
        self.assertIn(f"绑定 {self.site.site_id} 你的姓名", text)
        self.assertIn("日常打卡", text)
        self.assertIn("我的状态 — 查看个人完成情况", text)
        self.assertIn("群状态 — 查看全群完成情况", text)
        self.assertNotIn("使用指令（按重要顺序）", text)
        self.assertNotIn("/", text)

    def test_site_list_includes_complete_private_onboarding(self):
        other_site = bot.SiteConfig(site_id="other", name="其他网站", url="https://other.test", chat_ids=frozenset())
        with patch.object(bot, "SITES", (self.site, other_site)), patch.object(bot, "DEFAULT_SITE", self.site):
            text = bot.site_list_text()
        self.assertIn("首次使用（请私聊机器人）", text)
        self.assertIn(f"切换 {self.site.site_id}", text)
        self.assertIn(f"绑定 {self.site.site_id} 你的姓名", text)
        self.assertIn("打卡 灵修", text)

    def test_private_welcome_is_sent_only_once(self):
        fake_bot = SimpleNamespace()
        temporary_state = {"welcomed": {}, "bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "send") as send:
            self.assertTrue(bot.send_private_welcome_once(fake_bot, 1, 900, 42, False))
            self.assertFalse(bot.send_private_welcome_once(fake_bot, 1, 900, 42, False))
        send.assert_called_once_with(fake_bot, 1, 900, bot.welcome_text())
        self.assertIn("42", temporary_state["welcomed"])

    def test_private_welcome_skips_groups_and_special_contacts(self):
        fake_bot = SimpleNamespace()
        temporary_state = {"welcomed": {}, "bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "send") as send:
            self.assertFalse(bot.send_private_welcome_once(fake_bot, 1, 900, 42, True))
            self.assertFalse(bot.send_private_welcome_once(fake_bot, 1, 900, 5, False))
        send.assert_not_called()

    def test_checkin_retro_and_cancel_use_the_same_task_names(self):
        self.assertEqual(bot.parse_task_kind("灵修"), "每日灵修")
        self.assertEqual(bot.parse_task_kind("周读物"), "周读物")
        self.assertEqual(bot.parse_task_kind("视频"), "周视频")
        self.assertEqual(bot.parse_task_kind("背经"), "周背经")
        self.assertIsNone(bot.parse_task_kind("随便写"))
        task, logical_date = bot.parse_retro_command("补签 视频 2026-08-20", self.site)
        self.assertEqual(bot.parse_task_kind(task), "周视频")
        self.assertEqual(logical_date, "2026-08-20")
        task, logical_date = bot.parse_cancel_command("取消打卡 视频 2026-08-20", self.site)
        self.assertEqual(bot.parse_task_kind(task), "周视频")
        self.assertEqual(logical_date, "2026-08-20")

    def test_cancel_removes_all_records_that_keep_status_done(self):
        website_state = {
            "records": [
                {"Id": 7, "name": "张迦勒", "logical_date": "2026-08-18", "video": "done"},
                {"Id": 8, "name": "张迦勒", "logical_date": "2026-08-20", "video": "done"},
            ],
            "weeklySchedule": self.schedule,
        }
        with patch.object(bot, "website_snapshot", return_value=(website_state, {"weekly_schedule": self.schedule})), patch.object(bot, "delete_json") as delete:
            ok, result = bot.website_cancel_checkin(self.site, "张迦勒", "周视频", "2026-08-20")
        self.assertTrue(ok)
        self.assertIn("共 2 条记录", result)
        self.assertEqual([call.args[1] for call in delete.call_args_list], ["/api/checkins/7", "/api/checkins/8"])

    def test_admin_key_is_hashed_and_verifies_safely(self):
        record = create_key_record("Strong-Key-2026")
        self.assertNotIn("Strong-Key-2026", json.dumps(record))
        self.assertTrue(verify_key("Strong-Key-2026", record))
        self.assertFalse(verify_key("wrong-secret", record))
        with tempfile.TemporaryDirectory() as folder:
            path = bot.Path(folder) / "admin-key.json"
            save_key_record(path, record)
            self.assertEqual(load_key_record(path), record)

    def test_admin_verification_message_is_redacted_from_logs(self):
        self.assertEqual(bot.safe_log_text("管理员验证 Strong-Key-2026"), "管理员验证 ***")
        self.assertEqual(bot.safe_log_text("管理员 验证 Strong-Key-2026"), "管理员验证 ***")

    def test_admin_verification_binds_once_and_group_verification_is_forbidden(self):
        temporary_state = {"bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}}
        fake_bot = SimpleNamespace()
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "send") as send, patch.object(bot, "verify_admin_attempt", return_value=(True, "验证成功。")) as verify:
            handled = bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员验证 Strong-Key-2026")
            self.assertTrue(handled)
            self.assertTrue(bot.is_admin(77))
            verify.assert_called_once_with(77, "Strong-Key-2026")
            bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员验证 another-secret")
            verify.assert_called_once()
            send.reset_mock()
            bot.handle_admin_command(fake_bot, 1, 101, 88, True, self.site, "管理员验证 Strong-Key-2026")
            self.assertIn("只能私聊", send.call_args.args[3])

    def test_only_bound_admin_can_broadcast(self):
        fake_bot = SimpleNamespace()
        temporary_state = {"bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "send"), patch.object(bot, "broadcast_group_update") as broadcast:
            bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员 广播 测试公告")
            broadcast.assert_not_called()
            temporary_state["admins"]["77"] = {"verified_at": "2026-08-21T10:00:00+08:00"}
            broadcast.return_value = 1
            bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员 广播 测试公告")
            broadcast.assert_called_once_with(fake_bot, 1, self.site, "📢 测试网站 · 管理员公告\n\n测试公告")

    def test_health_heartbeat_is_accepted_by_container_check(self):
        with tempfile.TemporaryDirectory() as folder:
            health_file = bot.Path(folder) / "health.json"
            with patch.object(bot, "DATA_DIR", bot.Path(folder)), patch.object(bot, "HEALTH_FILE", health_file):
                bot.write_health()
            payload = json.loads(health_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "running")
            with patch.object(healthcheck, "HEALTH_FILE", health_file), patch.object(healthcheck, "MAX_AGE_SECONDS", 120):
                self.assertEqual(healthcheck.main(), 0)


if __name__ == "__main__":
    unittest.main()
