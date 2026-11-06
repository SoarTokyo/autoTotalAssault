# SPDX-License-Identifier: GPL-3.0-or-later
"""技能轴表格编辑和回放控制窗口。"""
import copy
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QSaveFile, QIODevice, Signal, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFileDialog, QMessageBox, QTableWidget, QTableWidgetItem, QPlainTextEdit, QSplitter, QGroupBox, QComboBox)

from .replay_log import format_event
from .replay_script import import_script, timer_ms, timer_text, validate_script


class WheelSafeComboBox(QComboBox):
    """收起时滚轮交给表格，展开后仍可正常浏览选项。"""
    def wheelEvent(self,event):
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class ReplayDialog(QDialog):
    busyChanged=Signal(bool)

    def __init__(self,parent,hwnd,keymap,*,embedded=False):
        super().__init__(parent)
        if embedded:self.setWindowFlags(Qt.WindowType.Widget)
        self.before_start=None
        self.setWindowTitle('技能轴编辑与复现')
        self.resize(1120,820)
        self._log_pending=b''
        self.hwnd,self.keymap=hwnd,keymap
        self.root=Path(__file__).resolve().parents[2]
        self.script=None
        self._identity_options_key=None
        self._identity_base_options=()
        self.source_path=None
        self.process=QProcess(self)
        self.process.setWorkingDirectory(str(self.root))
        env=QProcessEnvironment.systemEnvironment()
        env.insert('PYTHONIOENCODING','utf-8')
        self.process.setProcessEnvironment(env)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_log)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.error)
        self._pending_close=False
        self._stop_requested=False
        self.stop_timeout_ms=3000
        self.kill_timeout_ms=1000
        self.stop_timer=QTimer(self)
        self.stop_timer.setSingleShot(True)
        self.stop_timer.timeout.connect(self.terminate_stalled_replay)
        self.kill_timer=QTimer(self)
        self.kill_timer.setSingleShot(True)
        self.kill_timer.timeout.connect(self.kill_stalled_replay)
        from .gui_hotkey import StartHotkeyWatcher
        self.stop_hotkey_watcher=StartHotkeyWatcher('f9',self)
        self.stop_hotkey_watcher.timer.stop()
        self.stop_hotkey_watcher.pressed.connect(self.stop)
        layout=QVBoxLayout(self)
        if embedded:
            title=QLabel('技能轴编辑与复现')
            title.setStyleSheet('font-size: 24px; font-weight: 600; margin: 8px 0;')
            layout.addWidget(title)
        help_box=QGroupBox('使用说明')
        help_layout=QVBoxLayout(help_box)
        label=QLabel('1. 加载技能轴，修改倒计时、费用、卡槽与目标坐标；勾选“执行”决定是否使用该行。\n'
                     '2. 默认定时释放；“秒接上个”：无视录制的战斗倒计时，在该技能能放出来时立刻释放；“严格卡点”：为尽可能满足精确时间点释放，会提前选中技能牌。两项互斥。\n'
                     '3. “保存”写回当前轴，“另存为”保留新版本。开始前设置倍速并关闭 AUTO；轴中的 AUTO 开关由程序按时间设置，点击开始后切回游戏按 F8。\n'
                     '4. F9 或“立即停止”结束复现；下方查看时间详情。\n'
                     '5. “选择视觉配置”用于切换倒计时、费用和卡牌等识别模板，默认自动选择最新本地配置，通常无需更改。\n'
                     '6. 【转场】行用于跳过动画；AUTO开关行可选择开启或关闭，程序自动定位按钮并确认状态。【AUTO观察】仅记录游戏自行释放的技能，不发送手动施放输入。')
        label.setWordWrap(True)
        help_layout.addWidget(label)
        layout.addWidget(help_box)
        row=QHBoxLayout()
        self.load_button=QPushButton('加载分析结果或技能轴…')
        self.load_button.clicked.connect(self.load)
        self.save_button=QPushButton('保存')
        self.save_button.clicked.connect(self.save)
        self.save_as_button=QPushButton('另存为…')
        self.save_as_button.clicked.connect(self.save_as)
        self.profile_button=QPushButton('选择视觉配置…')
        self.profile_button.setToolTip('切换倒计时、费用、卡牌、选卡状态、AUTO 与倍速等识别所用的本地模板配置。默认自动选取最新本地配置，通常无需更改。')
        self.profile_button.clicked.connect(self.choose_profile)
        for b in (self.load_button,self.save_button,self.save_as_button,self.profile_button): row.addWidget(b)
        layout.addLayout(row)
        self.script_label=QLabel('尚未加载技能轴')
        self.script_label.setWordWrap(True)
        layout.addWidget(self.script_label)
        profiles=sorted((self.root/'profiles').glob('local-*/profile.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        self.profile=profiles[0] if profiles else None
        self.profile_label=QLabel(str(self.profile or '尚未选择视觉配置'))
        self.profile_label.setWordWrap(True)
        layout.addWidget(self.profile_label)
        self.table=QTableWidget(0,9)
        self.table.setHorizontalHeaderLabels(['执行','战斗倒计时','卡牌身份','卡槽','释放时费用','目标 X','目标 Y','秒接上个','严格卡点'])
        self.table.horizontalHeaderItem(4).setToolTip('释放时希望达到的当前 EX 费用，可输入小数，如 7.5。当前视觉识别仅提供整数，7.5 会等待识别到至少 8；另有独立的实际卡费检查。')
        self.table.itemChanged.connect(self.trigger_changed)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.details_splitter=QSplitter(Qt.Orientation.Vertical)
        self.details_splitter.addWidget(self.table)
        row=QHBoxLayout()
        self.add_button=QPushButton('新增动作（在选中行下）')
        self.add_button.setToolTip('在当前行之后插入；未选中行时添加到末尾。核对后勾选执行。')
        self.add_button.clicked.connect(self.add_action)
        self.delete_button=QPushButton('删除选中行')
        self.delete_button.clicked.connect(self.delete_actions)
        row.addWidget(self.add_button)
        row.addWidget(self.delete_button)
        self.start_button=QPushButton('开始复现')
        self.start_button.clicked.connect(self.start)
        self.stop_button=QPushButton('立即停止')
        self.stop_button.setToolTip('F9 与此按钮执行同一停止流程；子进程未及时退出时自动终止，无需再点一次。')
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.start_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.log=QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(20000)
        self.log.setMinimumHeight(200)
        details=QGroupBox('执行详情（输入时间与画面确认分开记录）')
        details_layout=QVBoxLayout(details)
        details_layout.addWidget(self.log)
        self.details_splitter.addWidget(details)
        self.details_splitter.setSizes([310,290])
        layout.addWidget(self.details_splitter,1)

    def load(self):
        directory=self.root/'scripts' if (self.root/'scripts').exists() else self.root/'analyses'
        path,_=QFileDialog.getOpenFileName(self,'选择 auto_actions.json 或战斗脚本',str(directory),'JSON (*.json)')
        if not path:return
        started=time.perf_counter()
        try:
            self.script=import_script(path,self.keymap.copy())
            self._identity_options_key=None
            # GUI统一使用当前游戏键位；历史录制及原轴文件不在加载时改写。
            self.script['keymap']=self.keymap.copy()
            self.table.blockSignals(True)
            self.table.setUpdatesEnabled(False)
            self.table.setRowCount(len(self.script['actions']))
            for row,action in enumerate(self.script['actions']):
                self.populate_row(row,action)
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
            self.table.resizeColumnsToContents()
            source=Path(path).resolve()
            raw=json.loads(source.read_text(encoding='utf-8'))
            self.source_path=source if 'algorithm' not in raw and not self.is_protected(source) else None
            self.script_label.setText(f'当前轴：{self.source_path}' if self.source_path else f'导入来源：{source}（保存时需另存为技能轴）')
            self.log.appendPlainText(f'已加载 {len(self.script["actions"])} 条动作，用时 {time.perf_counter()-started:.2f} 秒；回放采用主窗口当前键位设置。保存前请核对目标坐标和卡槽。')
        except Exception as error:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
            self.script=None
            self.source_path=None
            self.table.setRowCount(0)
            self.script_label.setText('加载失败，请重新选择技能轴')
            QMessageBox.warning(self,'加载失败',str(error))

    def populate_row(self,row,action):
        checkbox=QTableWidgetItem()
        checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
        checkbox.setCheckState(Qt.CheckState.Unchecked if action['ignored'] else Qt.CheckState.Checked)
        checkbox.setToolTip(action.get('notes',''))
        self.table.setItem(row,0,checkbox)
        conditions=action['trigger']['conditions']
        cost=next((c['value'] for c in conditions if c['field']=='ex_cost' and c['op']=='gte'),0)
        point=action.get('target',{}).get('point',[.5,.5])
        values=[timer_text(action['trigger']['remaining_ms']),
                action.get('skill',{}).get('identity') or '',
                str((action.get('skill',{}).get('slot') or 0)+1),str(cost),str(point[0]),str(point[1])]
        for col,value in enumerate(values,1):self.table.setItem(row,col,QTableWidgetItem(value))
        if action['trigger']['remaining_ms'] is None:
            self.table.item(row,1).setToolTip('本次倒计时未识别，请手动补填；不会用猜测的时间执行。')
        self.populate_identity(row,action.get('skill',{}).get('identity'))
        slot=WheelSafeComboBox()
        for number in range(3):slot.addItem(str(number+1),number)
        slot.setCurrentIndex(action.get('skill',{}).get('slot') or 0)
        slot.setToolTip('只能选择当前屏幕上的第 1、2、3 个技能卡槽，不是编队位置。')
        self.table.setCellWidget(row,3,slot)
        for col in (2,3):
            item=self.table.item(row,col)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        chain=QTableWidgetItem()
        chain.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
        chain.setCheckState(Qt.CheckState.Checked if action['trigger'].get('after_previous',False) else Qt.CheckState.Unchecked)
        chain.setToolTip('不等原轴时间，费用和冷却满足后尽快释放；与严格卡点互斥。')
        self.table.setItem(row,7,chain)
        strict=QTableWidgetItem()
        strict.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
        strict.setCheckState(Qt.CheckState.Checked if action['trigger'].get('strict_time',False) else Qt.CheckState.Unchecked)
        strict.setToolTip('目标前约1个游戏秒、卡费已足够时先提牌；重测子弹时间，到达目标才点击。提前容差为0，不改倍速，延后容差与释放成功判定保持原逻辑，与秒接互斥。')
        self.table.setItem(row,8,strict)
        if action['kind'] in ('set_auto','observe_auto_skill'):
            identity=self.table.cellWidget(row,2)
            identity.clear()
            if action['kind']=='set_auto':
                for text,value in [('待确认 AUTO 状态',None),('【AUTO】开启',True),('【AUTO】关闭',False)]:identity.addItem(text,value)
                identity.setCurrentIndex(1 if action.get('auto') is True else 2 if action.get('auto') is False else 0)
                slot.clear();slot.addItem('—',None)
                self.table.item(row,1).setToolTip('按本行倒计时设置 AUTO 目标状态；已处于目标状态时不点击，不会盲目反复切换。')
            else:
                checkbox.setCheckState(Qt.CheckState.Unchecked)
                identity.addItem('【AUTO观察】'+(action.get('skill',{}).get('identity') or '未识别'),None)
                identity.setEnabled(False)
                for col in range(9):self.table.item(row,col).setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
            slot.setEnabled(False)
            for col in (4,5,6):
                self.table.item(row,col).setText('—')
                self.table.item(row,col).setToolTip('AUTO 按钮按当前画面定位；游戏自行选择自动技能目标，不使用手动目标坐标或费用。')
            for col in (4,5,6,7,8):self.table.item(row,col).setFlags(Qt.ItemFlag.ItemIsEnabled)
        if action['kind']=='skip_transition':
            identity=self.table.cellWidget(row,2)
            identity.clear();identity.addItem('【转场】跳过动画',None);identity.setEnabled(False)
            slot.setEnabled(False)
            for col in (4,7,8):self.table.item(row,col).setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.table.item(row,1).setToolTip('转场检测窗口的开始时间；HUD消失后连点目标坐标，倒计时恢复立即停止。默认不启用，请核对证据和坐标。')
            for col in (5,6):self.table.item(row,col).setToolTip('转场“确认/是”按钮的归一化坐标。')

    def identity_options(self):
        team=self.script.get('selected_team') if self.script else None
        key=(tuple(s['content_id'] for s in team['students']) if team else None,str(self.profile))
        if key!=self._identity_options_key:
            base=set()
            if team:
                from .analysis_team import available_students
                from .special_skills import team_identity_options
                ids=set(key[0])
                selected=[s for s in available_students() if s['content_id'] in ids]
                base.update(team_identity_options(selected))
            elif self.profile:
                try:
                    data=json.loads(self.profile.read_text(encoding='utf-8'))
                    base.update(entry['label'] for entry in data.get('portraits',[]) if isinstance(entry.get('label'),str))
                except (OSError,ValueError,TypeError,KeyError):pass
            self._identity_base_options=tuple(sorted(base))
            self._identity_options_key=key
        values=set(self._identity_base_options)
        # 原轴已有身份保留，避免旧模板暂不可用时悄悄清空身份校验。
        if self.script:
            values.update(a.get('skill',{}).get('identity') for a in self.script['actions'] if a.get('skill',{}).get('identity'))
        return sorted(values)

    def populate_identity(self,row,current):
        identity=WheelSafeComboBox()
        identity.addItem('不限定（仅校验卡槽）',None)
        for value in sorted(set(self.identity_options()) | ({current} if current else set())):
            identity.addItem(value,value)
        identity.setCurrentIndex(max(0,identity.findData(current)))
        identity.setToolTip('新轴显示所选队伍及轴内已有的特殊卡牌身份；历史轴兼容原视觉配置。不限定时不检查角色身份。')
        self.table.setCellWidget(row,2,identity)

    def add_action(self):
        if self.script is None:
            QMessageBox.information(self,'请先加载','请先加载分析结果或技能轴，再新增动作。')
            return
        row=self.table.currentRow()+1 if self.table.currentRow()>=0 else self.table.rowCount()
        # 不复制历史证据，也不要求其余尚未改完的单元格已通过验证。
        remaining=0
        if row>0:
            try:remaining=timer_ms(self.table.item(row-1,1).text())
            except (ValueError,TypeError):pass
        action=dict(id=f'manual-{uuid4()}',kind='cast_skill',ignored=True,review_status='needs_review',
                    source_event_ids=[],source_frame_ids=[],historical_t_ns=None,
                    trigger=dict(remaining_ms=remaining,time_tolerance_ms=500,conditions=[],
                                 timeout_ms=10000,on_failure='abort'),
                    skill=dict(slot=0,identity=None),target=dict(kind='point',point=[.5,.5],identity=None),notes='')
        self.script['actions'].insert(row,action)
        self.table.blockSignals(True)
        try:
            self.table.insertRow(row)
            self.populate_row(row,action)
        finally:self.table.blockSignals(False)
        self.table.setCurrentCell(row,1)
        self.table.scrollToItem(self.table.item(row,1))
        self.table.editItem(self.table.item(row,1))
        self.log.appendPlainText(f'已新增第 {row+1} 行；请填写时间、费用、技能和目标，核对后勾选“执行”，最后保存。')

    def delete_actions(self):
        if self.script is None:return
        rows=sorted({index.row() for index in self.table.selectedIndexes()},reverse=True)
        if not rows:
            self.log.appendPlainText('请先选中需要删除的行；可按 Ctrl 或 Shift 多选。')
            return
        self.table.blockSignals(True)
        try:
            for row in rows:
                self.table.removeRow(row)
                del self.script['actions'][row]
        finally:self.table.blockSignals(False)
        self.log.appendPlainText(f'已删除 {len(rows)} 行（尚未保存）；请检查后续“秒接上个”是否仍连接正确的技能。')

    def trigger_changed(self,item):
        if item.column() in (7,8) and item.checkState()==Qt.CheckState.Checked:
            other=self.table.item(item.row(),15-item.column())
            if other is not None:other.setCheckState(Qt.CheckState.Unchecked)

    def edited(self):
        if self.script is None:raise ValueError('请先加载技能轴')
        data=copy.deepcopy(self.script)
        data['keymap']=self.keymap.copy()
        data['revision']+=1
        for row,action in enumerate(data['actions']):
            if action['kind']=='observe_auto_skill':
                action['ignored']=True
                continue
            action['ignored']=self.table.item(row,0).checkState()!=Qt.CheckState.Checked
            # 复杂条件保留在 JSON 中，表格只允许编辑简单费用条件。
            conditions=action['trigger']['conditions']
            if any(c['field']!='ex_cost' or c['op']!='gte' for c in conditions) or len(conditions)>1:
                raise ValueError('该轴包含复杂条件，请通过 JSON 编辑，表格不会覆盖这些条件')
            time_value=self.table.item(row,1).text().strip()
            action['trigger']['remaining_ms']=timer_ms(time_value) if time_value else None
            action['trigger']['after_previous']=self.table.item(row,7).checkState()==Qt.CheckState.Checked
            action['trigger']['strict_time']=self.table.item(row,8).checkState()==Qt.CheckState.Checked
            if action['kind']=='set_auto':
                action['auto']=self.table.cellWidget(row,2).currentData()
                action['trigger']['conditions']=[]
                action['review_status']='confirmed'
                continue
            if action['kind']=='skip_transition':
                action['trigger']['conditions']=[]
                action['target']['point']=[float(self.table.item(row,i).text()) for i in (5,6)]
                action['review_status']='confirmed'
                continue
            action.setdefault('skill',{}).update(identity=self.table.cellWidget(row,2).currentData(),
                                                  slot=self.table.cellWidget(row,3).currentData())
            action['trigger']['conditions']=[dict(field='ex_cost',op='gte',value=float(self.table.item(row,4).text()),
                                                    max_age_ms=350,min_confidence=.75)]
            action.setdefault('target',{}).update(kind='point',point=[float(self.table.item(row,i).text()) for i in (5,6)])
            action['review_status']='confirmed'
        validate_script(data)
        return data

    def save(self):
        if self.source_path is None:
            self.save_as()
            return
        try:
            if self.is_protected(self.source_path):raise ValueError('不能覆盖原始录制、分析结果或执行快照，请另存为技能轴')
            data=self.edited()
            target=QSaveFile(str(self.source_path))
            if not target.open(QIODevice.OpenModeFlag.WriteOnly):raise OSError(target.errorString())
            payload=json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')
            if target.write(payload)!=len(payload):
                target.cancelWriting()
                raise OSError(target.errorString())
            if not target.commit():raise OSError(target.errorString())
            self.script=data
            self.log.appendPlainText(f'已保存回当前轴：{self.source_path}')
        except Exception as error:QMessageBox.warning(self,'保存失败',str(error))

    def is_protected(self,target):
        return any(target.resolve().is_relative_to((self.root/name).resolve())
                   for name in ('recordings','analyses','exports','replays'))

    def save_as(self):
        try:
            data=self.edited()
            path,_=QFileDialog.getSaveFileName(self,'另存已核对技能轴',str(self.root/'scripts'/f"{data['script_id']}.json"),'JSON (*.json)')
            if not path:return
            target=Path(path)
            if target.exists():raise ValueError('请使用新文件名，保留已有版本')
            if self.is_protected(target):raise ValueError('请将技能轴保存在 scripts 目录，不要写入原始数据目录或执行快照')
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('x',encoding='utf-8') as handle:json.dump(data,handle,ensure_ascii=False,indent=2)
            self.source_path=target.resolve()
            self.script=data
            self.script_label.setText(f'当前轴：{self.source_path}')
            self.log.appendPlainText(f'已保存：{target}')
        except Exception as error:QMessageBox.warning(self,'保存失败',str(error))

    def choose_profile(self):
        path,_=QFileDialog.getOpenFileName(self,'选择已校准的视觉配置',str(self.root/'profiles'),'JSON (*.json)')
        if path:
            self.profile=Path(path)
            self._identity_options_key=None
            self.profile_label.setText(path)
            for row in range(self.table.rowCount()):
                if self.script['actions'][row]['kind'] in ('set_auto','observe_auto_skill','skip_transition'):continue
                current=self.table.cellWidget(row,2).currentData()
                self.populate_identity(row,current)

    def start(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:return
        if self.before_start is not None and not self.before_start():return
        try:
            data=self.edited()
            if not self.profile:raise ValueError('请先选择视觉配置')
            # 保存不可变执行快照；开始执行采用此时表格中的值。
            import uuid
            folder=self.root/'replays'/'requests'
            folder.mkdir(parents=True,exist_ok=True)
            path=folder/f'run-{uuid.uuid4()}.json'
            with path.open('x',encoding='utf-8') as handle:json.dump(data,handle,ensure_ascii=False,indent=2)
            executable=Path(sys.executable)
            if executable.name.lower()=='pythonw.exe':executable=executable.with_name('python.exe')
            args=['-u','-m','ba_recorder','replay',str(path),'--hwnd',str(self.hwnd),'--visual-profile',str(self.profile),'--execute']
            self._log_pending=b''
            self._stop_requested=False
            self.stop_timer.stop()
            self.kill_timer.stop()
            self.log.appendPlainText('—— 开始新一轮复现 ——')
            self.busy(True)
            self.process.start(str(executable),args)
        except Exception as error:QMessageBox.warning(self,'无法开始回放',str(error))

    def busy(self,value):
        if value:
            self.stop_hotkey_watcher.set_key('f9')
            self.stop_hotkey_watcher.timer.start()
        else:self.stop_hotkey_watcher.timer.stop()
        for widget in (self.table,self.load_button,self.save_button,self.save_as_button,self.profile_button,self.start_button,self.add_button,self.delete_button):
            widget.setEnabled(not value)
        self.stop_button.setEnabled(value)
        self.busyChanged.emit(value)

    def stop(self):
        if self._stop_requested:
            if self.process.state()==QProcess.ProcessState.NotRunning:self.finished()
            return
        if self.process.state()==QProcess.ProcessState.NotRunning:
            self.finished()
            return
        self.process.write(b'{"command":"stop"}\n')
        self.process.closeWriteChannel()
        if not self._stop_requested:
            self._stop_requested=True
            self.stop_button.setText('正在停止……')
            self.stop_button.setEnabled(False)
            self.log.appendPlainText('已请求停止复现，正在等待退出。')
            self.stop_timer.start(self.stop_timeout_ms)

    def terminate_stalled_replay(self):
        if self.process.state()==QProcess.ProcessState.NotRunning:return
        self.log.appendPlainText('复现子进程未及时退出，正在终止；未完成的证据写入可能不完整。')
        self.process.terminate()
        self.kill_timer.start(self.kill_timeout_ms)

    def kill_stalled_replay(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self.log.appendPlainText('复现子进程仍未退出，已强制结束。')
            self.process.kill()

    def read_log(self):
        self._log_pending+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self._log_pending:
            line,self._log_pending=self._log_pending.split(b'\n',1)
            self.handle_log_line(line.decode('utf-8',errors='replace').rstrip('\r'))

    def handle_log_line(self,line):
        if line.startswith('@replay '):
            try:
                event=json.loads(line[8:])
                text=format_event(event)
                if event.get('kind')=='termination' and self.process.state()!=QProcess.ProcessState.NotRunning:
                    # 热键停止或自然完成时，让控制线程收到 EOF，避免解释器退出锁等待。
                    self.process.closeWriteChannel()
                    if not self.stop_timer.isActive() and not self.kill_timer.isActive():
                        self.stop_timer.start(self.stop_timeout_ms)
            except (ValueError,TypeError,KeyError):
                text=line
        else:text=line
        if text:self.log.appendPlainText(text)

    def finished(self,*_):
        self.stop_timer.stop()
        self.kill_timer.stop()
        self.read_log()
        if self._log_pending:
            self.handle_log_line(self._log_pending.decode('utf-8',errors='replace'))
            self._log_pending=b''
        self.busy(False)
        self.stop_timer.stop()
        self.kill_timer.stop()
        self._stop_requested=False
        self.stop_button.setText('立即停止')
        if self._pending_close:
            self._pending_close=False
            QTimer.singleShot(0,self.close)

    def error(self,*_):
        self.log.appendPlainText(self.process.errorString())
        if self.process.state()==QProcess.ProcessState.NotRunning:self.finished()

    def closeEvent(self,event):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self._pending_close=True
            self.stop()
            self.log.appendPlainText('已请求停止，等待回放退出后再关闭。')
            event.ignore()
        else:event.accept()

    def keyPressEvent(self,event):
        if not self.isWindow() and event.key()==Qt.Key.Key_Escape:
            event.ignore()
            return
        super().keyPressEvent(event)
