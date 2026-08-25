import json
import os
import tempfile
import unittest
from datetime import date, datetime
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

    def test_website_poll_interval_is_fast_but_safely_bounded(self):
        self.assertGreaterEqual(bot.WEBSITE_POLL_INTERVAL, 2)
        self.assertLessEqual(bot.WEBSITE_POLL_INTERVAL, 60)

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

    def test_group_status_lists_member_names_in_website_order(self):
        website_state = {
            "members": ["甲", "乙", "丙"],
            "records": [
                {"name": "乙", "logical_date": "2026-08-21", "daily": "done"},
                {"name": "甲", "logical_date": "2026-08-21", "daily": "done"},
                {"name": "乙", "logical_date": "2026-08-18", "book": "done"},
            ],
            "weeklySchedule": self.schedule,
        }
        fixed_now = SimpleNamespace(date=lambda: date(2026, 8, 21))
        with patch.object(bot, "website_snapshot", return_value=(website_state, {"weekly_schedule": self.schedule})), patch.object(bot, "now", return_value=fixed_now):
            text = bot.website_status(self.site)
        self.assertIn("灵修/读经（2 人）\n甲、乙", text)
        self.assertIn("周读物（1 人）\n乙", text)
        self.assertIn("视频（0 人）\n暂无", text)

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

    def test_website_poll_baselines_then_notifies_new_checkin_and_retro(self):
        initial = {"records": [{"Id": 1, "name": "甲", "logical_date": "2026-08-21", "daily": "done"}]}
        updated = {"records": [
            *initial["records"],
            {"Id": 2, "name": "乙", "logical_date": "2026-08-22", "daily": "done"},
            {"Id": 3, "name": "丙", "logical_date": "2026-08-20", "book": "done", "is_retro": "yes"},
        ]}
        temporary_state = {
            "website_records": {}, "recent_announcements": {}, "bindings": {}, "active_sites": {},
            "checkins": {}, "reminded": {}, "admins": {}, "welcomed": {},
        }
        fake_bot = SimpleNamespace()
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "website_snapshot", side_effect=[(initial, {}), (updated, {})]), patch.object(bot, "build_group_update", side_effect=lambda site, name, kind, day, **kwargs: f"{name}|{kind}|{day}|retro={kwargs.get('retro')}"), patch.object(bot, "broadcast_group_update") as broadcast:
            self.assertEqual(bot.poll_website_notifications(fake_bot, 1, self.site), 0)
            self.assertEqual(bot.poll_website_notifications(fake_bot, 1, self.site), 2)
        messages = [call.args[3] for call in broadcast.call_args_list]
        self.assertIn("乙|每日灵修|2026-08-22|retro=False", messages)
        self.assertIn("丙|周读物|2026-08-20|retro=True", messages)

    def test_website_poll_migrates_legacy_baseline_without_false_cancellation(self):
        temporary_state = {
            "website_records": {self.site.site_id: ["id:1", "id:2"]}, "recent_announcements": {},
            "bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}, "welcomed": {},
        }
        remaining = {"records": [{"Id": 1, "name": "甲", "logical_date": "2026-08-21", "daily": "done"}]}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "website_snapshot", return_value=(remaining, {})), patch.object(bot, "broadcast_group_update") as broadcast:
            self.assertEqual(bot.poll_website_notifications(SimpleNamespace(), 1, self.site), 0)
        broadcast.assert_not_called()

    def test_website_poll_notifies_cancellation_with_operation_time(self):
        previous_summary = {
            "name": "乙", "logical_date": "2026-08-20", "types": ["每日灵修", "周视频"], "retro": True,
        }
        temporary_state = {
            "website_records": {self.site.site_id: {"id:2": previous_summary}}, "recent_announcements": {},
            "bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {}, "welcomed": {},
        }
        fixed_now = datetime(2026, 8, 22, 19, 45, 12, tzinfo=bot.ZoneInfo("Asia/Shanghai"))
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "website_snapshot", return_value=({"records": []}, {})), patch.object(bot, "now", return_value=fixed_now), patch.object(bot, "build_group_update", side_effect=lambda site, name, kind, day, **kwargs: f"{name}|{kind}|cancelled={kwargs.get('cancelled')}|time={kwargs.get('operation_time')}"), patch.object(bot, "broadcast_group_update") as broadcast:
            self.assertEqual(bot.poll_website_notifications(SimpleNamespace(), 1, self.site), 2)
        messages = [call.args[3] for call in broadcast.call_args_list]
        self.assertIn("乙|每日灵修|cancelled=True|time=2026-08-22 19:45:12", messages)
        self.assertIn("乙|周视频|cancelled=True|time=2026-08-22 19:45:12", messages)

    def test_cancel_group_update_includes_operation_time(self):
        with patch.object(bot, "checkin_timeline", return_value=("当天灵修（按时间）", [])):
            text = bot.build_group_update(
                self.site,
                "乙",
                "每日灵修",
                "2026-08-20",
                cancelled=True,
                operation_time="2026-08-22 19:45:12",
            )
        self.assertIn("乙取消了打卡：灵修", text)
        self.assertIn("操作时间：2026-08-22 19:45:12", text)

    def test_cancel_timeline_excludes_cancelled_member(self):
        records = [
            {"name": "甲", "logical_date": "2026-08-22", "daily": "done", "checkin_time": "2026-08-22T08:00:00+08:00"},
            {"name": "信择", "logical_date": "2026-08-22", "daily": "done", "checkin_time": "2026-08-22T19:43:00+08:00"},
        ]
        with patch.object(bot, "website_snapshot", return_value=({"records": records}, {})):
            _, rows = bot.checkin_timeline(self.site, "每日灵修", "2026-08-22", frozenset({"信择"}))
        self.assertEqual(rows, ["1. 08:00  甲"])

    def test_same_poll_sends_cancellation_before_recheck(self):
        temporary_state = {
            "website_records": {self.site.site_id: {"id:1": {"name": "信择", "logical_date": "2026-08-22", "types": ["每日灵修"], "retro": False}}},
            "recent_announcements": {}, "bindings": {}, "active_sites": {}, "checkins": {},
            "reminded": {}, "admins": {}, "welcomed": {},
        }
        current = {"records": [{"Id": 2, "name": "信择", "logical_date": "2026-08-22", "daily": "done", "checkin_time": "2026-08-22T19:44:00+08:00"}]}
        def build_message(site, name, kind, day, **kwargs):
            return "取消" if kwargs.get("cancelled") else "打卡"
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "website_snapshot", return_value=(current, {})), patch.object(bot, "build_group_update", side_effect=build_message), patch.object(bot, "broadcast_group_update") as broadcast:
            self.assertEqual(bot.poll_website_notifications(SimpleNamespace(), 1, self.site), 2)
        self.assertEqual([call.args[3] for call in broadcast.call_args_list], ["取消", "打卡"])

    def test_checkin_notification_never_says_checked_in_then_empty(self):
        with patch.object(bot, "checkin_timeline", return_value=("本周背经（按时间）", [])):
            text = bot.build_group_update(
                self.site,
                "信择",
                "周背经",
                "2026-08-22",
                event_time="08-22 19:35",
            )
        self.assertIn("信择打卡了：背经", text)
        self.assertIn("本次记录\n1. 08-22 19:35  信择", text)
        self.assertNotIn("暂无打卡", text)

    def test_help_keeps_status_commands_explicit(self):
        other_site = bot.SiteConfig(site_id="other", name="其他网站", url="https://other.test", chat_ids=frozenset())
        with patch.object(bot, "SITES", (self.site, other_site)), patch.object(bot, "DEFAULT_SITE", self.site):
            text = bot.help_text(self.site)
        self.assertIn("首次使用（请私聊）", text)
        self.assertIn("直接回复你所在的网站名称", text)
        self.assertIn("绑定 你的姓名", text)
        self.assertIn("日常打卡", text)
        self.assertIn("灵修 — 阅读当天灵修内容", text)
        self.assertIn("我的状态 — 查看个人完成情况", text)
        self.assertIn("我的月状态 — 查看个人本月月历", text)
        self.assertIn("群状态 — 查看全群完成情况", text)
        self.assertNotIn("使用指令（按重要顺序）", text)
        self.assertNotIn("/", text)

    def test_site_list_includes_complete_private_onboarding(self):
        other_site = bot.SiteConfig(site_id="other", name="其他网站", url="https://other.test", chat_ids=frozenset())
        with patch.object(bot, "SITES", (self.site, other_site)), patch.object(bot, "DEFAULT_SITE", self.site):
            text = bot.site_list_text()
        self.assertIn("请选择门训网站", text)
        self.assertIn("直接回复上面的网站名称", text)
        self.assertNotIn("网站代号", text)

    def test_find_site_accepts_friendly_chinese_names(self):
        sites = (
            bot.SiteConfig(site_id="zk", name="科大门训打卡", url="https://zk.test", chat_ids=frozenset()),
            bot.SiteConfig(site_id="tianlu", name="天路历程门训打卡", url="https://tianlu.test", chat_ids=frozenset()),
        )
        by_id = {site.site_id: site for site in sites}
        with patch.object(bot, "SITES", sites), patch.object(bot, "SITE_BY_ID", by_id):
            self.assertEqual(bot.find_site("科大").site_id, "zk")
            self.assertEqual(bot.find_site("天路历程").site_id, "tianlu")

    def test_member_month_status_matches_website_profile_calendar_rules(self):
        website_state = {
            "records": [
                {"name": "张迦勒", "logical_date": "2026-08-18", "book": "done"},
                {"name": "张迦勒", "logical_date": "2026-08-20", "daily": "done"},
                {"name": "张迦勒", "logical_date": "2026-08-19", "video": "done"},
            ],
            "weeklySchedule": self.schedule,
        }
        with patch.object(bot, "website_snapshot", return_value=(website_state, {"weekly_schedule": self.schedule})):
            text = bot.member_month_status(self.site, "张迦勒", "2026-08", today=date(2026, 8, 21))
        self.assertIn("张迦勒｜2026年8月", text)
        self.assertIn("灵修：1/21 天｜完整：1/21 天", text)
        self.assertIn("17日  🟨 2/4", text)
        self.assertIn("20日  ✅ 3/4", text)
        self.assertIn("21日  🟨 2/4", text)

    def test_member_month_status_rejects_future_month(self):
        with self.assertRaises(ValueError):
            bot.member_month_status(self.site, "张迦勒", "2026-09", today=date(2026, 8, 21))

    def test_member_history_summary_uses_all_records_and_deduplicates_tasks(self):
        website_state = {
            "records": [
                {"name": "张迦勒", "logical_date": "2026-08-18", "daily": "done", "book": "done"},
                {"name": "张迦勒", "logical_date": "2026-08-19", "daily": "done", "book": "done"},
                {"name": "张迦勒", "logical_date": "2026-08-20", "daily": "done", "video": "done", "is_retro": "yes"},
                {"name": "其他人", "logical_date": "2026-08-20", "daily": "done"},
            ],
            "weeklySchedule": self.schedule,
        }
        with patch.object(bot, "website_snapshot", return_value=(website_state, {"weekly_schedule": self.schedule})):
            text = bot.member_history_summary(self.site, "张迦勒", today=date(2026, 8, 21))
        self.assertIn("总进度：5/7 项（完成/需要）", text)
        self.assertIn("活跃：3 天｜1 个月", text)
        self.assertIn("灵修：3/4 天｜最长连续 3 天", text)
        self.assertIn("周读物：1/1 次", text)
        self.assertIn("视频：1/1 次", text)
        self.assertIn("背经：0/1 次", text)
        self.assertIn("补签记录：1 次", text)
        self.assertNotIn("其他人", text)

    def test_history_summary_excludes_future_unconfigured_and_pre_join_records(self):
        website_state = {"records": [
            {"name": "甲", "logical_date": "2025-01-01", "daily": "done", "is_retro": "yes"},
            {"name": "甲", "logical_date": "2026-08-24", "daily": "done", "book": "done"},
            {"name": "甲", "logical_date": "2026-08-30", "daily": "done"},
        ]}
        temporary_state = {"member_join_dates": {}}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "website_snapshot", return_value=(website_state, {})):
            text = bot.member_history_summary(self.site, "甲", today=date(2026, 8, 24))
        self.assertIn("统计周期：2026-08-24 至 2026-08-24", text)
        self.assertIn("总进度：1/1 项", text)
        self.assertIn("灵修：1/1 天", text)
        self.assertIn("周读物：0/0 次", text)
        self.assertIn("未计入异常记录：3 项", text)

    def test_configured_join_date_controls_history_denominator(self):
        website_state = {"records": [{"name": "甲", "logical_date": "2026-08-24", "daily": "done"}]}
        temporary_state = {"member_join_dates": {"test:甲": "2026-08-22"}}
        with patch.object(bot, "state", temporary_state), patch.object(bot, "website_snapshot", return_value=(website_state, {})):
            text = bot.member_history_summary(self.site, "甲", today=date(2026, 8, 24))
        self.assertIn("统计周期：2026-08-22 至 2026-08-24", text)
        self.assertIn("灵修：1/3 天", text)

    def test_warm_reminder_privately_messages_bound_member_and_rate_limits(self):
        temporary_state = {
            "bindings": {"77": {"test": "甲"}, "88": {"test": "乙"}},
            "warm_reminders": {},
        }
        fake_rpc = SimpleNamespace(create_chat_by_contact_id=lambda _accid, contact_id: contact_id + 1000)
        fake_bot = SimpleNamespace(rpc=fake_rpc, logger=SimpleNamespace(warning=lambda *_args: None))
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "send") as send, patch.object(bot.time, "time", return_value=100000):
            ok, result = bot.send_warm_reminder(fake_bot, 1, self.site, 77, "乙")
            self.assertTrue(ok)
            self.assertIn("已把暖心提醒", result)
            self.assertEqual(send.call_args.args[2], 1088)
            self.assertIn("甲想温柔地提醒你", send.call_args.args[3])
            ok, result = bot.send_warm_reminder(fake_bot, 1, self.site, 77, "乙")
            self.assertFalse(ok)
            self.assertIn("6 小时", result)

    def test_extracts_numbered_daily_devotion(self):
        markdown = "# 前言\n\n## 43\n\n**今日经文**\n\n这是今天的内容。\n\n## 44\n\n明天内容。"
        devotion = {
            "path": "/newtestament.md",
            "mode": "numbered",
            "numbered_start_date": "2026-05-27",
            "numbered_start": 43,
        }
        text = bot.extract_devotion_section(markdown, devotion, date(2026, 5, 27))
        self.assertIn("今日经文", text)
        self.assertIn("这是今天的内容", text)
        self.assertNotIn("明天内容", text)

    def test_extracts_date_daily_devotion(self):
        markdown = "八月二十一日\n昨天内容\n\n八月二十二日\n**今日经文**\n今天的灵修内容。\n\n八月二十三日\n明天内容"
        devotion = {"path": "/Kuangye.md", "mode": "date"}
        text = bot.extract_devotion_section(markdown, devotion, date(2026, 8, 22))
        self.assertIn("今天的灵修内容", text)
        self.assertNotIn("昨天内容", text)
        self.assertNotIn("明天内容", text)

    def test_numbered_mode_falls_back_to_date_heading(self):
        markdown = "八月二十二日\n今天的科大灵修。\n\n八月二十三日\n明天内容。"
        devotion = {"path": "/Kuangye.md", "mode": "numbered", "numbered_start": 1}
        text = bot.extract_devotion_section(markdown, devotion, date(2026, 8, 22))
        self.assertIn("今天的科大灵修", text)
        self.assertNotIn("明天内容", text)

    def test_daily_devotion_uses_selected_website_config(self):
        config = {"task_sections": {"daily": {"devotion": {"title": "旷野的筵席", "path": "/Kuangye.md", "mode": "date"}}}}
        markdown = "八月二十二日\n这是当天内容。\n\n八月二十三日\n下一天内容。"
        with patch.object(bot, "website_snapshot", return_value=({}, config)), patch.object(bot, "fetch_text", return_value=markdown):
            text = bot.daily_devotion_text(self.site, date(2026, 8, 22))
        self.assertIn("旷野的筵席", text)
        self.assertIn("这是当天内容", text)

    def test_daily_devotion_highlights_quoted_scripture(self):
        lines = ["「你们要常在我里面，我也常在你们里面。」", "解释内容。", "**「已经加粗的经文」**", "“另一处经文”"]
        text = bot.clean_devotion_markdown(lines)
        self.assertIn("**「你们要常在我里面，我也常在你们里面。」**", text)
        self.assertIn("**「已经加粗的经文」**", text)
        self.assertNotIn("****「已经加粗的经文」****", text)
        self.assertIn("**“另一处经文”**", text)

    def test_send_uses_html_for_bold_and_plain_text_fallback(self):
        sent = []
        fake_bot = SimpleNamespace(rpc=SimpleNamespace(send_msg=lambda *args: sent.append(args)))
        bot.send(fake_bot, 1, 9, "标题\n**「经文」**\n解释")
        self.assertEqual(len(sent), 1)
        message = sent[0][2]
        self.assertEqual(message.text, "标题\n「经文」\n解释")
        self.assertIn("<strong>「经文」</strong>", message.html)
        self.assertIn("<br>", message.html)

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

    def test_bound_admin_can_publish_daily_devotion_from_group(self):
        fake_bot = SimpleNamespace()
        temporary_state = {"bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {
            "77": {"verified_at": "2026-08-21T10:00:00+08:00"}
        }}
        with patch.object(bot, "state", temporary_state), patch.object(
            bot, "publish_daily_devotion", return_value=1
        ) as publish, patch.object(bot, "send"):
            handled = bot.handle_admin_command(fake_bot, 1, 101, 77, True, self.site, "管理员 发布灵修")
            self.assertTrue(handled)
            publish.assert_called_once_with(fake_bot, 1, self.site)

    def test_manual_devotion_publish_marks_today_so_scheduler_skips_it(self):
        temporary_state = {"reminded": {}}
        fixed_now = datetime(2026, 8, 25, 5, 30)
        fake_bot = SimpleNamespace(logger=SimpleNamespace(exception=lambda *_args: None))
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "now", return_value=fixed_now), patch.object(bot, "send") as send:
            delivered = bot.publish_daily_devotion(fake_bot, 1, self.site, "今日灵修")
        self.assertEqual(delivered, 1)
        send.assert_called_once_with(fake_bot, 1, 101, "今日灵修")
        self.assertIn("test:2026-08-25:devotion:101", temporary_state["reminded"])

    def test_admin_can_bind_group_from_private_chat(self):
        temporary_state = {"bindings": {}, "active_sites": {}, "checkins": {}, "reminded": {}, "admins": {
            "77": {"verified_at": "2026-08-21T10:00:00+08:00"}
        }}
        group = SimpleNamespace(chat_type=bot.ChatType.GROUP, self_in_group=True)
        fake_bot = SimpleNamespace(rpc=SimpleNamespace(get_full_chat_by_id=lambda _accid, _chat_id: group))
        updated = bot.SiteConfig(site_id="test", name="测试网站", url="https://example.test", chat_ids=frozenset({101}))
        with patch.object(bot, "state", temporary_state), patch.object(bot, "bind_group_to_site", return_value=updated) as bind_group, patch.object(bot, "send") as send:
            handled = bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员 绑定群 101")
            self.assertTrue(handled)
            bind_group.assert_called_once_with(self.site, 101)
            self.assertIn("立即生效", send.call_args.args[3])

    def test_admin_can_set_member_join_date_from_private_chat(self):
        temporary_state = {"admins": {"77": {}}, "member_join_dates": {}}
        fake_bot = SimpleNamespace()
        fixed_now = datetime(2026, 8, 24, 10, 0)
        with patch.object(bot, "state", temporary_state), patch.object(bot, "save_state"), patch.object(bot, "website_snapshot", return_value=({"members": ["甲"]}, {})), patch.object(bot, "now", return_value=fixed_now), patch.object(bot, "send") as send:
            handled = bot.handle_admin_command(fake_bot, 1, 900, 77, False, self.site, "管理员 设置加入日期 甲 2026-08-01")
            self.assertTrue(handled)
            self.assertEqual(temporary_state["member_join_dates"]["test:甲"], "2026-08-01")
            self.assertIn("立即按新起点", send.call_args.args[3])

    def test_binding_group_updates_sites_file_and_memory_routes(self):
        with tempfile.TemporaryDirectory() as folder:
            sites_file = bot.Path(folder) / "sites.json"
            sites_file.write_text(json.dumps({"sites": [bot.site_config_row(self.site)]}, ensure_ascii=False), encoding="utf-8")
            with patch.dict(os.environ, {"MENXUN_SITES_JSON": ""}), patch.object(bot, "SITES_FILE", sites_file), patch.object(bot, "SITES", (self.site,)), patch.object(bot, "SITE_BY_ID", {"test": self.site}), patch.object(bot, "SITE_BY_CHAT_ID", {}), patch.object(bot, "DEFAULT_SITE", self.site):
                updated = bot.bind_group_to_site(self.site, 345)
                self.assertIn(345, updated.chat_ids)
                self.assertEqual(bot.SITE_BY_CHAT_ID[345].site_id, "test")
                saved = json.loads(sites_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["sites"][0]["chat_ids"], [101, 345])

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
