# 第三方组件与来源说明

本文件是当前源码的依赖与调研来源声明，不代替二进制发行包应附带的完整许可证文本、版权通知及对应源码。

本项目原创部分采用 GPL-3.0-or-later，范围见根目录 `COPYRIGHT.md`，完整许可证见 `LICENSE`。第三方组件继续适用各自许可，游戏识别素材不包含在公开源码运行包中；另行网盘分享不因此免除相应权利要求。运行库精确版本见 `requirements-win64.lock.txt`；下列盘点是来源记录，不是完整合规保证。

## 当前实际使用的库

| 组件 | 用途 | 当前安装版本的许可信息 |
| --- | --- | --- |
| mss | Windows 屏幕采集 | MIT |
| pynput | 普通系统键鼠监听与输入 | LGPLv3 |
| pywin32 | 窗口几何、焦点和进程查询 | 安装包元数据标为 PSF，具体文件声明另核对 |
| NumPy | 图像数组与数值计算 | BSD-3-Clause 及随包第三方许可 |
| opencv-python / OpenCV | 图像识别、视频兼容编码 | 安装包元数据标为 Apache 2.0；捆绑依赖另核对 |
| PySide6 / Qt / shiboken6 | 桌面界面 | LGPL/GPL/商业许可，按实际模块和分发方式履约 |
| PyAV | FFmpeg 的 Python 接口 | BSD-3-Clause；FFmpeg 和编解码器不因此变成 BSD |
| jsonschema | JSON 轴和会话验证 | MIT |

详细版本、安装包提供的许可证文件位置及 FFmpeg 构建探测见 [依赖盘点](docs/dependency_inventory.json)。运行 `python tools/audit_dependencies.py` 可重新生成。该清单只覆盖指定组件，不是完整传递依赖 SBOM。

若分发包含这些库、DLL 或 Python 运行时的成品，需从对应版本提取完整许可和版权文件，并核对重链接/替换库、对应源码与安装信息等适用义务。尤其不能仅凭 PyAV 的 BSD 元数据就忽略 FFmpeg、x264 等组件。

## 调研与致谢

### 学生图库来源

学生图库下载来源：[GameKee 蔚蓝档案 Wiki 实装学生栏目](https://www.gamekee.com/ba/second/23941)。感谢 Wiki 维护者及参与资料、图片整理的贡献者。

本项目维护者已就此下载方式与 GameKee 蔚蓝档案 Wiki 维护方沟通并获得同意。本项说明根据项目维护者提供的沟通结果记录，仅涵盖所沟通的下载方式；使用时保持低频、分批请求，尊重限流及后续站点要求，不绕过访问限制。

站方同意不代表游戏官方对本工具的认可，不为游戏图片再分发、商业使用、其他来源图片或多形态技能素材包提供额外授权。游戏美术、角色与商标的权利仍属于相应权利人。多形态技能识别素材的公开获取与分发方案仍待完善，不能将此项下载同意套用于该素材库。

### 开源项目

本项目设计阶段研究了下列项目，感谢其公开的实现与文档：

- [BAAS](https://github.com/pur1fying/blue_archive_auto_script)：GPL-3.0。
- [BAAS_Cpp](https://github.com/pur1fying/BAAS_Cpp)：GPL-3.0。
- [GO_BAAS](https://github.com/pur1fying/GO_BAAS)：早期核查版本含 GPLv3 正文。
- [BAAH](https://github.com/BlueArchiveArisHelper/BAAH)：MIT。

当前没有将上述项目列为运行依赖，也未发现随当前源码分发的其 OCR 客户端、C++ 库、模型或资源包。本项目的 mss/Win32 层、字形识别、语义解析和回放模块是本仓库实现；与 BAAS 使用同一通用依赖，不等于引入 BAAS 源码。本项依据与局限见 [许可证审查](docs/licensing_review.md)。

致谢不等于获得额外授权，也不代替未来实际复用代码时必须保留的许可证和版权声明。游戏截图、角色图像和商标的权利不因相关工具开源而转移。
