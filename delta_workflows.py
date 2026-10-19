"""Delta Chat 的引导式群绑定与全组统计。移植自 Tato-menxunbot c23666e。"""
from datetime import date, timedelta
import re
import time
import unicodedata

class DeltaWorkflows:
    def __init__(self, core, bot, accid):
        self.core, self.bot, self.accid = core, bot, accid
        self.group_binding_flows = {}

    def discover_groups(self):
        entries = self.bot.rpc.get_chatlist_entries(self.accid, None, None, None)
        changed = False
        for chat_id in entries:
            chat = self.bot.rpc.get_full_chat_by_id(self.accid, chat_id)
            if chat.chat_type != self.core.ChatType.GROUP or not chat.self_in_group:
                continue
            key = str(chat_id)
            with self.core.state_lock:
                types = self.core.state.setdefault('delta_chat_types', {})
                names = self.core.state.setdefault('delta_chat_names', {})
                if types.get(key) != 2 or names.get(key) != chat.name:
                    types[key], names[key] = 2, chat.name
                    changed = True
        if changed:
            with self.core.state_lock:
                self.core.save_state()

    def handle(self, cid, uid, text):
        clean = text.strip().rstrip('！!。. ')
        if clean in {'管理员 绑定群', '管理员绑定群', '管理员 解绑群', '管理员解绑群', '管理员 群列表', '管理员群列表'} and self.core.is_super_admin(uid):
            self.discover_groups()
        if clean in {'管理员 群列表', '管理员群列表'} and self.core.is_super_admin(uid):
            lines = ['🛡️ 机器人所在的群聊']
            for group_id, kind in self.core.state.get('delta_chat_types', {}).items():
                if kind != 2:
                    continue
                site = self.core.SITE_BY_CHAT_ID.get(int(group_id))
                lines.append(f"{group_id} · {self._group_name(group_id)}｜{site.name if site else '未绑定'}")
            self.core.send(self.bot, self.accid, cid, '\n'.join(lines))
            return True
        if self._handle_group_binding_flow(cid, uid, text):
            return True
        if self._handle_potato_admin_summary(cid, uid, text):
            return True
        return self._send_private_summary(cid, uid, text)

    def _cedar_sites(self):
        return [site for site in self.core.SITES if self.core.is_cedar_site(site)]

    def _group_name(self, group_id):
        return str(self.core.state.get('delta_chat_names', {}).get(str(group_id), '')).strip()

    def _binding_label(self, site):
        return site.name.rsplit('/', 1)[-1].strip()

    def _match_name(self, value):
        value = unicodedata.normalize('NFKC', value).casefold().replace('侍奉', '事奉').replace('🚪训', '门训')
        value = value.rsplit('/', 1)[-1]
        for word in ('yds', 'hz', 'jh', '个人', '门训', '小组', '通知', '群聊', '群', '组'):
            value = value.replace(word, '')
        return ''.join((char for char in value if char.isalnum()))

    def _suggest_site(self, group_id):
        key = self._match_name(self._group_name(group_id))
        if len(key) < 2:
            return None
        matches = []
        for site in self._cedar_sites():
            site_key = self._match_name(self._binding_label(site))
            if site_key and (key == site_key or key in site_key or site_key in key):
                matches.append(site)
        return matches[0] if len(matches) == 1 else None

    def _binding_groups(self, mode):
        if mode == 'bind':
            return sorted((int(group_id) for group_id, kind in self.core.state.get('delta_chat_types', {}).items() if int(kind) in {2, 3} and int(group_id) not in self.core.SITE_BY_CHAT_ID))
        return sorted((group_id for group_id, site in self.core.SITE_BY_CHAT_ID.items() if self.core.is_cedar_site(site)))

    def _flow_group_list(self, mode, groups):
        title = '🧭 选择要绑定的群（回复编号）' if mode == 'bind' else '🧭 选择要解绑的 Cedar 通知群（回复编号）'
        lines = [title]
        for index, group_id in enumerate(groups, 1):
            name = self._group_name(group_id)
            label = f' · {name}' if name else ''
            site = self._suggest_site(group_id) if mode == 'bind' else self.core.SITE_BY_CHAT_ID.get(group_id)
            hint = f' → 建议：{self._binding_label(site)}' if mode == 'bind' and site else f' → {self._binding_label(site)}' if site else ''
            lines.append(f'{index}. {group_id}{label}{hint}')
        lines.append('回复“取消”退出。')
        return '\n'.join(lines)

    def _flow_site_list(self):
        lines = ['请选择 Cedar 小组（回复编号）：']
        lines.extend((f'{index}. {self._binding_label(site)}' for index, site in enumerate(self._cedar_sites(), 1)))
        lines.append('回复“取消”退出。')
        return '\n'.join(lines)

    def _start_group_binding_flow(self, cid, uid, mode):
        self.group_binding_flows.pop(cid, None)
        if not self.core.is_super_admin(uid):
            self.core.send(self.bot, self.accid, cid, 'Cedar 通知群绑定与解绑仅限超级管理员。')
            return
        if not self._cedar_sites():
            self.core.send(self.bot, self.accid, cid, '尚未配置 Cedar 小组。')
            return
        groups = self._binding_groups(mode)
        if not groups:
            message = '没有待绑定的群。请先把机器人加入群，并在群里发送任意消息。' if mode == 'bind' else '目前没有已绑定的 Cedar 通知群。'
            self.core.send(self.bot, self.accid, cid, message)
            return
        self.group_binding_flows[cid] = {'uid': uid, 'mode': mode, 'stage': 'group', 'groups': groups, 'expires': time.monotonic() + 900}
        self.core.send(self.bot, self.accid, cid, self._flow_group_list(mode, groups))

    def _handle_group_binding_flow(self, cid, uid, text):
        clean = text.strip().rstrip('！!。.')
        if re.fullmatch('管理员\\s*绑定群', clean):
            self._start_group_binding_flow(cid, uid, 'bind')
            return True
        if re.fullmatch('管理员\\s*解绑群', clean):
            self._start_group_binding_flow(cid, uid, 'unbind')
            return True
        flow = self.group_binding_flows.get(cid)
        if not flow:
            return False
        if clean.startswith('管理员'):
            self.group_binding_flows.pop(cid, None)
            return False
        if flow['uid'] != uid or not self.core.is_super_admin(uid):
            self.group_binding_flows.pop(cid, None)
            self.core.send(self.bot, self.accid, cid, '该操作仅限发起流程的超级管理员。')
            return True
        if time.monotonic() > flow['expires']:
            self.group_binding_flows.pop(cid, None)
            self.core.send(self.bot, self.accid, cid, '操作已超时。请重新发送“管理员 绑定群”或“管理员 解绑群”。')
            return True
        if clean in {'取消', '退出'}:
            self.group_binding_flows.pop(cid, None)
            self.core.send(self.bot, self.accid, cid, '已取消，群绑定未变更。')
            return True
        if flow['stage'] == 'group':
            groups = flow['groups']
            group_id = groups[int(clean) - 1] if clean.isdigit() and 1 <= int(clean) <= len(groups) else int(clean) if clean.isdigit() and int(clean) in groups else None
            if group_id is None:
                self.core.send(self.bot, self.accid, cid, self._flow_group_list(flow['mode'], groups))
                return True
            flow['group_id'] = group_id
            if flow['mode'] == 'unbind':
                site = self.core.SITE_BY_CHAT_ID.get(group_id)
                if not site or not self.core.is_cedar_site(site):
                    self.group_binding_flows.pop(cid, None)
                    self.core.send(self.bot, self.accid, cid, '该群的 Cedar 绑定已变化，请重新开始。')
                    return True
                flow['site_id'] = site.site_id
            else:
                site = self._suggest_site(group_id)
                if not site:
                    flow['stage'] = 'site'
                    self.core.send(self.bot, self.accid, cid, self._flow_site_list())
                    return True
                flow['site_id'] = site.site_id
            flow['stage'] = 'confirm'
            site = self.core.SITE_BY_ID[flow['site_id']]
            verb = '绑定到' if flow['mode'] == 'bind' else '从此小组解绑'
            option = '；回复“其他”选择小组' if flow['mode'] == 'bind' else ''
            self.core.send(self.bot, self.accid, cid, f"群：{group_id} · {self._group_name(group_id) or '未命名'}\nCedar 小组：{self._binding_label(site)}\n确认{verb}？回复“确认”{option}，或回复“取消”。")
            return True
        if flow['stage'] == 'site':
            sites = self._cedar_sites()
            site = sites[int(clean) - 1] if clean.isdigit() and 1 <= int(clean) <= len(sites) else self.core.find_site(clean)
            if not site or not self.core.is_cedar_site(site):
                self.core.send(self.bot, self.accid, cid, self._flow_site_list())
                return True
            flow['site_id'] = site.site_id
            flow['stage'] = 'confirm'
            self.core.send(self.bot, self.accid, cid, f"群：{flow['group_id']} · {self._group_name(flow['group_id']) or '未命名'}\nCedar 小组：{self._binding_label(site)}\n确认绑定？回复“确认”，或回复“取消”。")
            return True
        if clean == '其他' and flow['mode'] == 'bind':
            flow['stage'] = 'site'
            self.core.send(self.bot, self.accid, cid, self._flow_site_list())
            return True
        if clean != '确认':
            self.core.send(self.bot, self.accid, cid, '请回复“确认”或“取消”；绑定时也可回复“其他”重新选择小组。')
            return True
        self.group_binding_flows.pop(cid, None)
        site = self.core.SITE_BY_ID.get(flow['site_id'])
        if not site or not self.core.is_cedar_site(site):
            self.core.send(self.bot, self.accid, cid, '小组配置已变化，请重新开始。')
            return True
        group_id = flow['group_id']
        try:
            if flow['mode'] == 'bind':
                group_chat = self.bot.rpc.get_full_chat_by_id(self.accid, group_id)
                if group_chat.chat_type != self.core.ChatType.GROUP or not group_chat.self_in_group:
                    raise ValueError('机器人已不在该群，请重新加入后再绑定。')
                self.core.bind_group_to_site(site, group_id)
                result = '绑定'
            else:
                self.core.unbind_group_from_site(site, group_id)
                result = '解绑'
            self.core.send(self.bot, self.accid, cid, f'✅ 已{result}：群 {group_id} · {self._binding_label(site)}。配置已保存并立即生效。')
        except (ValueError, RuntimeError, OSError) as error:
            self.core.send(self.bot, self.accid, cid, f'❌ {error}')
        return True

    def _percent(self, done, total):
        return f'{done * 100 / total:.1f}%' if total else '0.0%'

    def _message_chunks(self, message, max_length=3500):
        """按行拆分长总结，给 Delta Chat 的单条消息长度留出余量。"""
        chunks = []
        current = []
        current_length = 0
        for line in message.splitlines():
            addition = len(line) + (1 if current else 0)
            if current and current_length + addition > max_length:
                chunks.append('\n'.join(current))
                current = []
                current_length = 0
            current.append(line)
            current_length += len(line) + (1 if current_length else 0)
        if current:
            chunks.append('\n'.join(current))
        return chunks or [message]

    def _broadcast_summary(self, site, message):
        delivered = 0
        for group_chat_id in sorted(site.chat_ids):
            try:
                for index, chunk in enumerate(self._message_chunks(message), 1):
                    suffix = f'\n\n（第 {index}/{len(self._message_chunks(message))} 段）' if len(self._message_chunks(message)) > 1 else ''
                    self.core.send(self.bot, self.accid, group_chat_id, chunk + suffix)
                delivered += 1
            except Exception as error:
                self.bot.logger.exception('发送群总结失败：site=%s chat_id=%s error=%s', site.site_id, group_chat_id, error)
        return delivered

    def _weekly_group_summary(self, site):
        website_state, site_config = self.core.website_snapshot(site)
        members = [str(x).strip() for x in website_state.get('members') or [] if str(x).strip()]
        records = website_state.get('records') or []
        schedule = website_state.get('weeklySchedule') or site_config.get('weekly_schedule') or []
        current = self.core.now(site).date()
        plan = self.core.current_week(schedule, current)
        week_start = date.fromisoformat(str(plan.get('start'))) if plan else current - timedelta(days=current.weekday())
        week_end = min(current, date.fromisoformat(str(plan.get('end'))) if plan else current)
        week_days = [week_start + timedelta(days=index) for index in range((week_end - week_start).days + 1)]
        tasks = [('每日灵修', '灵修')]
        for task, label in (('周读物', '周读物'), ('周视频', '视频'), ('周背经', '背经')):
            if plan and self.core.weekly_task_value(plan, task):
                tasks.append((task, label))
        category = []
        member_rows = []
        overall_done = 0
        overall_required = len(members) * (len(week_days) + len(tasks) - 1)
        task_results = {}
        daily_results = {name: {day for day in week_days if any((self.core.website_record_matches(row, name, '每日灵修', day, schedule, site) for row in records))} for name in members}
        task_results['每日灵修'] = daily_results
        daily_done = sum((len(days) for days in daily_results.values()))
        daily_required = len(members) * len(week_days)
        all_daily = [name for name, days in daily_results.items() if len(days) == len(week_days)]
        partial_daily = [f'{name}（{len(days)}/{len(week_days)}）' for name, days in daily_results.items() if 0 < len(days) < len(week_days)]
        category.extend([f'【灵修】本周累计：{daily_done}/{daily_required} 次｜完成率 {self._percent(daily_done, daily_required)}', f"全程完成：{'、'.join(all_daily) or '暂无'}", f"进行中：{'、'.join(partial_daily) or '暂无'}", ''])
        for task, label in tasks[1:]:
            completed = [name for name in members if any((self.core.website_record_matches(row, name, task, week_start, schedule, site) for row in records))]
            task_results[task] = set(completed)
            missing = [name for name in members if name not in completed]
            category.extend([f'【{label}】本周：{len(completed)}/{len(members)} 人｜完成率 {self._percent(len(completed), len(members))}', f"已完成：{'、'.join(completed) or '暂无'}", f"待完成：{'、'.join(missing) or '无（全员完成）'}", ''])
        for name in members:
            daily_count = len(daily_results[name])
            weekly_count = sum((name in task_results[task] for task, _ in tasks[1:]))
            completed = daily_count + weekly_count
            required = len(week_days) + len(tasks) - 1
            overall_done += completed
            icon = '✅' if completed == required else '🟡' if completed else '⭕'
            buttons = f'灵修 {daily_count}/{len(week_days)}（{self._percent(daily_count, len(week_days))}）'
            if tasks[1:]:
                buttons += '\n' + ' '.join((f"{label}{('✅' if name in task_results[task] else '❌')}" for task, label in tasks[1:]))
            member_rows.append(f'{icon} {name}：{completed}/{required} 项｜{self._percent(completed, required)}\n{buttons}\n────────────')
        week_label = str((plan or {}).get('title') or '本周门训')
        return '\n'.join([f'📊 {site.name}｜本周全组总结', f'统计区间：{week_start.isoformat()} 至 {week_end.isoformat()}', f'本周主题：{week_label}', '', f'【总体】{overall_done}/{overall_required} 项｜完成率 {self._percent(overall_done, overall_required)}', f"全程完成：{sum((row.startswith('✅') for row in member_rows))}/{len(members)} 人", '', '一、各项完成情况', *category, '二、个人完成情况', '说明：✅全部完成｜🟡部分完成｜⭕尚未完成', '────────────', *member_rows])

    def _history_group_summary(self, site):
        history_start = date(2026, 4, 6)
        website_state, site_config = self.core.website_snapshot(site)
        members = [str(x).strip() for x in website_state.get('members') or [] if str(x).strip()]
        stats = []
        total_done = total_required = 0
        task_totals = {label: [0, 0] for label in ('灵修', '周读物', '视频', '背经')}
        for name in members:
            text = self.core.member_history_summary(site, name, start_date=history_start, snapshot=(website_state, site_config))
            match = re.search('总进度：(\\d+)/(\\d+) 项', text)
            done, required = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
            total_done += done
            total_required += required
            breakdown = {}
            for label in ('灵修', '周读物', '视频', '背经'):
                item = re.search(f'^{label}：(\\d+)/(\\d+)', text, re.MULTILINE)
                values = (int(item.group(1)), int(item.group(2))) if item else (0, 0)
                breakdown[label] = values
                task_totals[label][0] += values[0]
                task_totals[label][1] += values[1]
            stats.append((done / required if required else 0, name, done, required, breakdown))
        current = self.core.now(site).date().isoformat()
        rows = []
        for index, (ratio, name, done, required, breakdown) in enumerate(sorted(stats, key=lambda item: (-item[0], item[1])), 1):
            icon = '🟢' if ratio >= 0.8 else '🟡' if ratio >= 0.6 else '🟠' if ratio >= 0.4 else '🔴'
            buttons = f"灵修 {breakdown['灵修'][0]}/{breakdown['灵修'][1]}｜周读物 {breakdown['周读物'][0]}/{breakdown['周读物'][1]}\n视频 {breakdown['视频'][0]}/{breakdown['视频'][1]}｜背经 {breakdown['背经'][0]}/{breakdown['背经'][1]}"
            rows.append(f'{index}. {icon} {name}：{done}/{required} 项｜{self._percent(done, required)}\n{buttons}\n────────────')
        task_rows = [f'【{label}】{done}/{required}｜{self._percent(done, required)}' for label, (done, required) in task_totals.items()]
        return '\n'.join([f'📈 {site.name}｜全组历史总结', f'统计区间：{history_start.isoformat()} 至 {current}', f'参与统计：{len(members)} 人', '', f'【总体】{total_done}/{total_required} 项｜完成率 {self._percent(total_done, total_required)}', '', '一、各打卡按钮完成分析', *task_rows, '', '二、个人明细（按总完成率排序）', '说明：🟢≥80%｜🟡60–79%｜🟠40–59%｜🔴<40%', '────────────', *rows])

    def _send_private_summary(self, cid, uid, text):
        normalized = text.strip().replace(' ', '').rstrip('！!。.')
        if normalized not in {'本周总结', '本周门训总结', '全组本周总结', '历史总结', '全组历史总结'}:
            return False
        _, site = self.core.resolve_message_site(self.bot, self.accid, cid, uid)
        if not site:
            self.core.send(self.bot, self.accid, cid, '请先发送“网站”，再选择需要统计的网站。')
            return True
        try:
            message = self._weekly_group_summary(site) if normalized in {'本周总结', '本周门训总结', '全组本周总结'} else self._history_group_summary(site)
            chunks = self._message_chunks(message)
            for index, chunk in enumerate(chunks, 1):
                suffix = f'\n\n（第 {index}/{len(chunks)} 段）' if len(chunks) > 1 else ''
                self.core.send(self.bot, self.accid, cid, chunk + suffix)
        except Exception:
            self.bot.logger.exception('私聊生成门训总结失败')
            self.core.send(self.bot, self.accid, cid, '生成总结失败，请检查网站连接后重试。')
        return True

    def _handle_potato_admin_summary(self, cid, uid, text):
        if not re.match('^管理员(?:\\s|本周|历史)', text.strip()):
            return False
        normalized = re.sub('^管理员\\s*', '', text.strip()).replace(' ', '')
        if normalized not in {'本周总结', '本周门训总结', '历史总结', '历史门训总结'}:
            return False
        _, site = self.core.resolve_message_site(self.bot, self.accid, cid, uid)
        if not site:
            self.core.send(self.bot, self.accid, cid, '请先发送“网站”，再选择需要统计的网站。')
            return True
        if not self.core.is_admin(uid, site):
            self.core.send(self.bot, self.accid, cid, f'你没有 {site.name} 的管理权限。')
            return True
        try:
            message = self._weekly_group_summary(site) if normalized in {'本周总结', '本周门训总结'} else self._history_group_summary(site)
            delivered = self._broadcast_summary(site, message)
            self.core.send(self.bot, self.accid, cid, f'✅ 总结已发送到 {delivered} 个通知群。' if delivered else '该网站还没有配置通知群。')
        except Exception:
            self.bot.logger.exception('生成门训总结失败')
            self.core.send(self.bot, self.accid, cid, '生成总结失败，请检查网站连接后重试。')
        return True
