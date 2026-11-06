# 开源准备与许可证核查

原核查日期：2026-10-29；许可选择更新：2026-11-05。项目所有者已选择 GPL-3.0-or-later，见根目录 LICENSE 与 COPYRIGHT.md；当前公开包不含游戏识别素材。下文保留原始来源核查及其局限，没有因此宣称外部依赖和游戏素材已全部获授权。

## 当前是否大量复用 BAAS

目前未发现此情形。检查 `src/` 的 Python 导入、`pyproject.toml`、安装脚本和工具入口，未发现 BAAS/BAAS_Cpp/GO_BAAS/BAAH 导入、动态服务调用或随包上游模块：

| 本项目组件 | 实际实现/依赖 | 与 BAAS 的关系 |
| --- | --- | --- |
| `windows.py` | Win32 客户区、PID、焦点查询 | 独立适配器，不导入 BAAS 窗口类 |
| `recorder.py` | mss、pynput、单调时钟与队列 | 未调用 BAAS Screenshot/Control |
| `deterministic_vision.py`、`card_state.py` | OpenCV、NumPy、本地字形及模板 | 未集成 BAAS OCR 客户端、OCR 服务或模型 |
| `replay.py`、`replay_timing.py` | pynput 和本仓库的调度/校准 | 未链接 BAAS_Cpp auto_fight 状态机 |
| `tools/calibrate_deterministic.py` | 自录画面的字形/图像校准 | 素材来源记录在本地 profile，不是下载 BAAS 模型 |

早期源码调研也明确选择独立实现、避免整体导入，见 `open_source_research.md`。这里只是现有文件与开发记录的核查，**导入扫描不能证明从未改写过任何上游代码**；若还有未记录的外部代码、DLL、模型或素材，需要补充其来源和许可证，不能据此直接宣布全部文件无许可义务。

## 是否需要另行取得作者许可

许可义务取决于复用内容和分发方式，没有“超过多少行才需要声明”的通用界线。

- 若仅研究设计并独立实现，没有复制受保护代码或资源，不因功能相似或同用 OpenCV/mss 就自动成为 BAAS 的 GPL 衍生版本。
- 若实际复制、修改或组合了 GPL 代码，并按 GPL 条件分发，一般不必另求逐项许可；但须履行适用的保留声明、标注修改、提供许可及对应源码等要求。若要采用不兼容的闭源/其他授权方式，应向有权授权的权利人取得额外授权，不能假定仓库维护者能代表所有贡献者与第三方。
- GPL 衍生/组合范围取决于实际结合方式。不能把“进程隔离”当成自动豁免，也不能只加一句致谢就替代 GPL。
- 私下运行和修改，与向他人分发，是不同情形；GPL 通常不要求把纯私用修改公开。
- 如果未来使用 BAAH 的 MIT 代码，应保留对应版权与许可文本；许可证本身通常已提供按条件使用的授权，不是只写项目名就够。

依据：[BAAS](https://github.com/pur1fying/blue_archive_auto_script)、[BAAS_Cpp](https://github.com/pur1fying/BAAS_Cpp)、[BAAH](https://github.com/BlueArchiveArisHelper/BAAH) 仓库许可标识，以及 [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.en.html#GPLRequireSourcePostedPublic)。上游逐文件声明和固定版本仍需保留，早期核查提交见 `research_manifest.json`。本次部分固定提交的 LICENSE 网页未能重新获取，没有据此声称重新核对了其每一行。

## 实际依赖的发布义务

当前真正需要准备的是通用库的分发许可，而不是虚构 BAAS 已被大范围集成。已有根目录 `THIRD_PARTY_NOTICES.md` 和可再生成的 `dependency_inventory.json`。主许可证现为 GPL-3.0-or-later，但不能把这项选择当作第三方依赖及素材合规审查已经完成的证明。

特别注意：

- Qt/PySide6 采用 LGPL/GPL/商业许可组合，按使用模块和打包形式审查；不是用了 PySide6 就可以删除 Qt 的许可文件。[Qt 官方说明](https://www.qt.io/development/open-source-lgpl-obligations)
- PyAV 自身为 BSD-3-Clause，但 FFmpeg 与编解码器各有许可；FFmpeg 官方明确区分默认 LGPL 与启用 GPL 组件的构建。[FFmpeg 官方许可说明](https://www.ffmpeg.org/legal.html)、[PyAV](https://github.com/PyAV-Org/PyAV)
- 本机 PyAV 16.1.0 的 avcodec DLL 自报 `LGPL version 3 or later`，构建标志包含 `--enable-libx264`、没有检测到 `--enable-gpl`。这不是完整授权结论：发布前必须核对构建配方、x264 来源/授权和其他 DLL，不能仅以 DLL 自报许可或 PyAV 元数据认定整包已合规，也不据此指控其违规。
- 源码仓库不应顺手打包 `.venv`、`.tools`、wheelhouse、游戏录制与校准素材。若发布 EXE/完整环境，须对实际产物重新盘点完整依赖、许可全文、对应源码及适用的动态库替换义务，当前声明文件不是成品许可证包。

## 本轮可移植性整理

新录制入口固定连续视频，删除 UI 的 PNG 频率、PNG 保存和前/后画面保留项。旧会话、底层兼容字段和原始录制未改写。分析/回放所需的少量证据图片与“录制整段 PNG”不同，仍保留。

UI 默认保存目录为 `recordings`，按项目根目录解析；项目内选定的目录和新写入的 profile 引用尽量保存相对路径，用户主动选择的外部目录仍可用绝对路径。运行时将路径解析为绝对路径是正常文件操作，不是绑定开发者电脑。Windows 字体目录从系统环境变量获取。

目前整理针对源码仓库运行，不等同于完成 wheel、单文件 EXE 或安装器发布。个人录制和旧日志里已有的绝对路径保持原样，避免修改历史数据；发布时不要包含这些文件。现有忽略规则已排除它们，若以后使用 Git，还须检查它们是否曾被显式跟踪。
