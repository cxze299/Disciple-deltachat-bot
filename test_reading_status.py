import unittest
import json
import tempfile
from pathlib import Path
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch
import menxun_bot as ref
import logging
bot = SimpleNamespace(bot=SimpleNamespace(logger=logging.getLogger('reading-test')))


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.site = ref.SiteConfig('test', '测试组', 'https://example.test', frozenset())
        self.snapshot = ({'members': ['甲','乙','丙'], 'records': [
            {'name':'甲','logical_date':'2026-09-27','task_type':'daily_scripture'},
            {'name':'甲','logical_date':'2026-09-27','task_type':'daily_scripture'},
            {'name':'甲','logical_date':'2026-09-26','task_type':'daily_scripture'},
            {'name':'乙','logical_date':'2026-09-27','book':'done'},
            {'name':'外组成员','logical_date':'2026-09-27','task_type':'daily_scripture'},
        ]}, {'task_sections':{'daily':{'scripture':{'enabled':True}}}})
        for p in [patch.object(ref,'website_snapshot',return_value=self.snapshot),
                  patch.object(ref,'now',return_value=datetime(2026,9,27))]:
            p.start(); self.addCleanup(p.stop)

    def test_personal_dates_deduplicate_and_exclude_other_tasks(self):
        text = ref.reading_status(self.site,'甲')
        self.assertIn('已打卡 2 天',text)
        self.assertIn('2026-09-25 ⬜ 无打卡记录',text)
        self.assertIn('今日打卡：⬜ 未打卡',ref.reading_status(self.site,'乙'))
        self.assertIn('2026-08-29',ref.reading_status(self.site,'甲',30))
        self.assertNotIn('2026-08-28',ref.reading_status(self.site,'甲',30))

    def test_group_scoped_members_and_empty(self):
        text = ref.reading_status(self.site)
        self.assertIn('今日完成：1/3 人',text)
        self.assertIn('未读（2 人）：乙、丙',text)
        self.assertNotIn('外组成员',text)
        with patch.object(ref,'website_snapshot',return_value=({},self.snapshot[1])):
            self.assertIn('暂无成员名单',ref.reading_status(self.site))

    def run_command(self,command,bound='甲',fail=False):
        event=SimpleNamespace(msg=SimpleNamespace(chat_id=900,from_id=77,text=command))
        with patch.object(ref,'resolve_message_site',return_value=(False,self.site)), \
             patch.object(ref,'send_private_welcome_once'), patch.object(ref,'handle_admin_command',return_value=False), \
             patch.object(ref,'find_site',return_value=None), patch.object(ref,'bound_name',return_value=bound), \
             patch.object(ref,'send') as send:
            if fail:
                with patch.object(ref,'website_snapshot',side_effect=RuntimeError('offline')):
                    ref.on_new_message(bot.bot,1,event)
            else:
                ref.on_new_message(bot.bot,1,event)
            self.assertTrue(send.called)
            return send.call_args.args[3]

    def test_devotion_never_counts_as_reading(self):
        self.snapshot[0]['records'] = [{'name':'甲','logical_date':'2026-09-27','daily':'done','task_type':'daily_devotion'}]
        self.assertIn('今日打卡：⬜ 未打卡',ref.reading_status(self.site,'甲'))
        self.assertFalse(ref.scripture_record_done({'daily':'done'}))
        self.assertTrue(ref.scripture_record_done({'scripture':'done'}))
        self.assertTrue(ref.scripture_record_done({'每日读经':'已完成'}))
        self.assertIsNone(ref.parse_task_kind('读经'))

    def test_disabled_website_falls_back_without_using_devotion(self):
        self.snapshot[1]['task_sections']['daily']['scripture']['enabled'] = False
        with patch.object(ref,'reading_plan_binding',return_value={}):
            self.assertIn('尚未关联',ref.reading_status(self.site,'甲'))
        with patch.object(ref,'reading_plan_binding',return_value={'plan_id':'test'}), patch.object(ref,'weidu_report',return_value={date(2026,9,27):{'甲':True,'乙':False}}):
            text=ref.reading_status(self.site)
            self.assertIn('微读圣经',text)
            self.assertIn('已读（1 人）：甲',text)
            self.assertIn('未读（1 人）：乙',text)
            self.assertIn('状态未知（1 人）：丙',text)
        with patch.object(ref,'reading_plan_binding',return_value={'plan_id':'test'}), patch.object(ref,'weidu_report',side_effect=ValueError('unavailable')):
            self.assertIn('暂时无法查询',ref.reading_status(self.site))

    def test_weidu_uses_reported_day_id_and_separate_done_undone(self):
        class Response:
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps(self.payload).encode()
        requests=[]
        def fake_open(request, timeout):
            requests.append(request)
            if '/list/' in request.full_url:
                return Response({'errno':200,'data':[{'dayTime':1790352000,'dayId':187}]})
            return Response({'errno':200,'data':{'done':[{'nickname':'甲'}],'undone':[{'nickname':'乙'}]}})
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary,'weidu-token.json').write_text(json.dumps({'x_ws_token':'test-token'}))
            with patch.object(ref,'DATA_DIR',Path(temporary)), patch.object(ref,'urlopen',side_effect=fake_open):
                reports=ref.weidu_report(self.site,{'plan_id':'48fe875d-4162-4968-8e9b-8f6b04f99eeb'},7)
        self.assertEqual(reports[date(2026,9,26)],{'甲':True,'乙':False})
        self.assertIn('dayId=187',requests[1].full_url)
        self.assertTrue(all(r.get_header('X-ws-token')=='test-token' for r in requests))

    def test_website_reading_takes_priority(self):
        with patch.object(ref,'reading_plan_binding') as binding:
            self.assertIn('网站独立读经',ref.reading_status(self.site))
            binding.assert_not_called()

    def test_all_command_routes_reply(self):
        for cmd in ['读经状态','我的读经状态','个人读经状态','每日读经状态','微读状态','我的读经记录','个人读经记录','读经记录','小组读经状态','群读经状态']:
            with self.subTest(cmd=cmd):
                self.assertIn('测试组',self.run_command(cmd))
        self.assertIn('最近 30 天',self.run_command('我的读经记录'))
        self.assertIn('请先绑定',self.run_command('读经状态',bound=''))
        self.assertIn('今日完成',self.run_command('小组读经状态',bound=''))
        self.assertIn('暂时无法连接',self.run_command('读经状态',fail=True))

    def test_delta_message_event_reaches_reading_handler(self):
        self.assertIn('最近 7 天：已打卡 2 天', self.run_command('读经状态'))

if __name__ == '__main__': unittest.main()
