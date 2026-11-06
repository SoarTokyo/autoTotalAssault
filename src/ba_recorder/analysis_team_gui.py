# SPDX-License-Identifier: GPL-3.0-or-later
"""分析前确认六人队伍；不进行全图库图像识别。"""
import re
from PySide6.QtCore import Qt,QModelIndex,QSortFilterProxyModel
from PySide6.QtGui import QStandardItem,QStandardItemModel
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QGridLayout,QGroupBox,QLabel,
    QComboBox,QCompleter,QDialogButtonBox,QPushButton)

from .analysis_team import normalize,validate_team


class StudentFilter(QSortFilterProxyModel):
    def __init__(self,parent=None):
        super().__init__(parent);self.terms=[]

    def search(self,text):
        self.terms=[normalize(word) for word in re.split(r'[\s()（）_-]+',text) if normalize(word)]
        self.invalidate()

    def filterAcceptsRow(self,row,parent):
        index=self.sourceModel().index(row,0,parent)
        search=index.data(Qt.ItemDataRole.UserRole+1) or ''
        return all(term in search for term in self.terms)


class StudentPicker(QComboBox):
    def __init__(self,students,parent=None):
        super().__init__(parent)
        self.setEditable(True);self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumWidth(230);self.setMaxVisibleItems(12)
        self.students={s['content_id']:s for s in students};self.selected_id=None
        model=QStandardItemModel(self)
        for student in students:
            label=student['name']+(f' / {student["english_name"]}' if student['english_name'] else '')
            if student.get('downloaded') is False:label+='（缺图，将下载）'
            item=QStandardItem(label);item.setData(student['content_id'],Qt.ItemDataRole.UserRole)
            item.setData(student['search_text'],Qt.ItemDataRole.UserRole+1);model.appendRow(item)
        self.setModel(model);self.setCurrentIndex(-1)
        self.lineEdit().setPlaceholderText('搜索国服名或英文名，然后选择')
        self.proxy=StudentFilter(self);self.proxy.setSourceModel(model)
        completer=QCompleter(self.proxy,self)
        completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.setCompleter(completer)
        completer.activated[QModelIndex].connect(lambda index:self.set_student(index.data(Qt.ItemDataRole.UserRole)))
        self.activated.connect(lambda _:self.set_student(self.currentData()))
        self.lineEdit().textEdited.connect(self.search)

    def search(self,text):
        self.selected_id=None
        self.proxy.search(text)

    def set_student(self,ident):
        if ident not in self.students:return
        self.setCurrentIndex(self.findData(ident));self.selected_id=ident

    def wheelEvent(self,event):
        if self.view().isVisible():super().wheelEvent(event)
        else:event.ignore()


class AnalysisTeamDialog(QDialog):
    Refresh=2
    def __init__(self,parent,students,previous=None):
        super().__init__(parent)
        self.setWindowTitle('确认本次录制的六人队伍');self.resize(980,360)
        self.students=students;self.result_team=None
        layout=QVBoxLayout(self)
        hint=QLabel('按暂停菜单的位置选择六名学生。支持国服名称和英文名称搜索，输入后请从候选中选择。\n'
                    '确认后只补下载所选学生缺少的普通图片；特殊素材缺失会提示后继续分析，相关阶段保留待核对。')
        hint.setWordWrap(True);layout.addWidget(hint)
        row=QHBoxLayout();left=QGroupBox('STRIKER · 前四名');right=QGroupBox('SPECIAL · 后两名')
        grid=QGridLayout(left);special=QGridLayout(right)
        self.pickers=[]
        for i in range(6):
            box=QGroupBox(f'位置 {i+1}');column=QVBoxLayout(box)
            picker=StudentPicker(students,self);self.pickers.append(picker);column.addWidget(picker)
            if i<4:grid.addWidget(box,i//2,i%2)
            else:special.addWidget(box,i-4,0)
        row.addWidget(left,2);row.addWidget(right,1);layout.addLayout(row)
        self.error=QLabel();self.error.setWordWrap(True);self.error.setStyleSheet('color:#b03030');layout.addWidget(self.error)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        refresh=QPushButton('刷新学生目录');refresh.clicked.connect(lambda:self.done(self.Refresh))
        layout.addWidget(refresh)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('确认队伍并开始分析')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        entries=previous.get('students',[]) if isinstance(previous,dict) else []
        if isinstance(entries,list):
            for picker,entry in zip(self.pickers,entries):
                if isinstance(entry,dict):picker.set_student(entry.get('content_id'))

    def accept(self):
        try:
            self.result_team=validate_team(dict(schema_version='1.0',students=[dict(content_id=p.selected_id)
                                             for p in self.pickers]),self.students)
        except ValueError as error:
            self.error.setText(str(error));return
        super().accept()
