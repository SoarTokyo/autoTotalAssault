# SPDX-License-Identifier: GPL-3.0-or-later
"""不需要编辑JSON的中文键位设置窗口。"""
from dataclasses import replace
import json
from pathlib import Path

from PySide6.QtCore import QSaveFile,QIODevice
from PySide6.QtWidgets import QDialog,QVBoxLayout,QFormLayout,QGroupBox,QLabel,QComboBox,QDialogButtonBox,QPushButton

from .keybindings import ACTION_LABELS,OPTIONAL_ACTIONS,key_choices,canonical_key,validate_bindings


def save_config(path,config):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    target=QSaveFile(str(path))
    if not target.open(QIODevice.OpenModeFlag.WriteOnly):raise OSError(target.errorString())
    payload=json.dumps(config.to_dict(),ensure_ascii=False,indent=2).encode('utf-8')
    if target.write(payload)!=len(payload):
        target.cancelWriting();raise OSError('键位设置写入失败，原配置未替换')
    if not target.commit():raise OSError(target.errorString())


class KeyBindingsDialog(QDialog):
    def __init__(self,parent,config,defaults,settings_path=None):
        super().__init__(parent)
        self.setWindowTitle('键位设置')
        self.resize(540,650)
        self.original=config;self.defaults=defaults;self.settings_path=settings_path;self.result_config=None
        layout=QVBoxLayout(self)
        hint=QLabel('请选择与游戏内设置一致的单个按键；这里不会修改游戏内键位。\n'
                    '默认 Esc 共用取消技能与菜单；若单独设置，请将共用键改为“未设置”。\n'
                    '回放控制固定为 F8 启动/继续、F9 停止。')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.editors={}
        group=QGroupBox('游戏快捷键');form=QFormLayout(group)
        for action,label in ACTION_LABELS.items():
            box=QComboBox()
            if action in OPTIONAL_ACTIONS:box.addItem('未设置',None)
            for text,key in key_choices():box.addItem(text,key)
            self.editors[action]=box;form.addRow(label,box)
        layout.addWidget(group)
        group=QGroupBox('录制控制热键');form=QFormLayout(group)
        for field,label in [('start_key','开始录制'),('stop_key','停止并保存'),('abort_key','重启录制（丢弃本段）')]:
            box=QComboBox()
            for i in range(1,13):
                if i!=10:box.addItem(f'F{i}',f'f{i}')
            self.editors[field]=box;form.addRow(label,box)
        layout.addWidget(group)
        self.error_label=QLabel();self.error_label.setWordWrap(True);self.error_label.setStyleSheet('color:#b42318')
        layout.addWidget(self.error_label)
        restore=QPushButton('恢复 Steam 默认键位');restore.clicked.connect(lambda:self.populate(self.defaults));layout.addWidget(restore)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存并应用')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.apply);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.populate(config)

    def populate(self,config):
        for field,box in self.editors.items():
            value=config.keymap.get(field) if field in ACTION_LABELS else getattr(config,field)
            if value is not None:value=canonical_key(value)
            index=box.findData(value)
            if index<0:
                box.addItem(f'自定义键（{value}）',value);index=box.count()-1
            box.setCurrentIndex(index)
        self.error_label.clear()

    def configured(self):
        mapping=self.original.keymap.copy()
        for action in ACTION_LABELS:
            value=self.editors[action].currentData()
            if value is None:mapping.pop(action,None)
            else:mapping[action]=value
        controls={field:self.editors[field].currentData() for field in ('start_key','stop_key','abort_key')}
        return validate_bindings(replace(self.original,keymap=mapping,**controls)).for_recording()

    def apply(self):
        try:
            config=self.configured()
            if self.settings_path:save_config(self.settings_path,config)
            self.result_config=config
            self.accept()
        except Exception as error:self.error_label.setText(str(error))
