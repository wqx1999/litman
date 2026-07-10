<h1>LITerature MANager</h1>

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo-hero-dark.png"/>
  <img src="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo-hero.png" width="52%" alt="litman"/>
</picture>

<p>
<a href="https://pypi.org/project/litman/"><img src="https://img.shields.io/pypi/v/litman?logo=pypi&logoColor=white" alt="PyPI version"/></a>
<img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
<img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/>
<img src="https://img.shields.io/badge/AI--native-Claude%20Code-D97757?logo=anthropic&logoColor=white" alt="AI-native: Claude Code"/>
</p>

<p><a href="README.md">English</a> | <b>中文</b></p>

</div>

**本地优先、AI 增强的文献管理器。**

一个面向研究论文的本地知识库，以纯文件形式存储在你的磁盘上。论文通过结构化
metadata 和符号链接，显式地与项目、代码仓库以及彼此关联。日常通过 Web UI 浏览、
阅读、批注；UI 覆盖不到的操作，直接问 AI agent——内置的 Claude Code skill 会让它
替你驱动完整的 `lit` CLI。

---

## 使用前须知

几件值得先了解的事：

1. **不要手动移动 vault 或项目文件夹。** 维系它的符号链接、项目桥接和 registry
   都是基于路径的；确需移动就在事后运行 `lit health-check` 修复损坏的部分。
2. **图表阅读需要多模态模型。** 纯文本模型会回退到纯文本抽取，看不到图或基于
   图像的表格。
3. **不要手动编辑 metadata 文件。** 修改论文、taxonomy 和配置，走 Web UI 或直接
   问 AI agent——两者底层都经过 `lit` 命令校验。
4. **Windows 用户。** 基于符号链接的功能（浏览视图、项目桥接）需要管理员权限；
   推荐使用 [WSL](https://learn.microsoft.com/en-us/windows/wsl/)。

## 核心特性

1. **你拥有的纯文件。** 整个库都是磁盘上的纯文本——YAML metadata、markdown
   笔记、原始 PDF。没有云数据库，不锁定：随处备份，整个库随手 `grep`。

2. **设计上保持一致。** 共享的 `TAXONOMY.md` 治理主题、方法、项目和数据来源；
   原子写入加 `lit health-check` 让交叉引用随库增长始终干净。

3. **论文 ↔ 项目 ↔ 代码。** 一篇论文可绑定多个项目（每个项目获得符号链接目录
   和自动生成的 `REFERENCES.md`），也可绑定克隆进 vault 的官方代码仓库——一张
   无需手动维护的显式知识图。

4. **Web UI + AI agent，共用一套校验内核。** 日常在 Web UI（`lit gui`）浏览、
   阅读、批注；更深的操作用自然语言问 Claude Code，内置的 `lit-library` /
   `lit-reading` skill 会驱动完整 CLI。每一次写入都经过校验，即使模型不完美，
   你的库依然正确。

---

## 安装

litman 是一个 Python CLI 工具。一行命令即可安装它及其所需的一切：

**macOS / Linux：**

```bash
curl -LsSf https://raw.githubusercontent.com/wqx1999/litman/main/install.sh | sh
```

**Windows**（PowerShell）：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/wqx1999/litman/main/install.ps1 | iex"
```

然后运行一次性安装向导：

```bash
lit setup   # 交互式向导：shell 补全 → Claude Code skill → vault 设置 →（可选）云同步 → 桌面快捷方式
```

升级请运行 `lit self-update`，或者重跑安装命令。

litman 每天会向 PyPI 查询一次最新版本号，你的版本过旧时会提示一句。设置
`LITMAN_NO_UPDATE_CHECK=1` 可关闭。不发送任何遥测数据。

**备选：pipx**

更习惯用 [pipx](https://pipx.pypa.io)？

```bash
pipx install litman   # 首次安装
pipx upgrade litman   # 更新
```

**从本地克隆安装**（开发）：

```bash
# 首次安装
git clone https://github.com/wqx1999/litman.git
cd litman
pipx install .

# 更新（先拉取最新代码）
git pull
pipx install --force .
```

## 卸载

分两步，注意顺序——先 `lit uninstall`（趁 `lit` 命令还在），再按你的安装方式删除 CLI：

```bash
lit uninstall              # 删除内置 skill、桌面快捷方式、shell 补全、vault 注册表、agent 偏好
uv tool uninstall litman   # 若用 uv / 安装脚本安装
pipx uninstall litman      # 若用 pipx 安装
```

`lit uninstall` 会先列出将删除的内容并请你确认——加 `--dry-run` 预览、加 `-y` 跳过。

如果是从本地克隆安装的，删完 CLI 后再删掉克隆下来的仓库目录：

```bash
rm -rf path/to/litman   # 你 git clone 的那个目录
```

以上都不会碰你的 vault（论文、PDF、笔记、标注）；如果连数据也要删，请手动删除那个目录。

## 快速上手

```bash
lit gui             # 打开 Web UI —— 浏览、阅读、批注、打标签、链接项目
lit gui --window    # 同上，但开成独立应用窗口（无地址栏）
lit agent           # 在库目录里启动你的 AI agent（claude 等）
```

`lit gui` 会自动打开浏览器（`--no-browser` 可关；无图形界面的机器上只打印
URL 和 SSH 隧道命令）。想要双击图标启动，跑一次 `lit gui --make-shortcut`。

就这样——`lit setup` 已经帮你建好了 vault。Web UI 覆盖日常的浏览、阅读、批注；
更多操作（添加论文、编辑 taxonomy、链接项目）用 `lit agent` 启动 agent 后用
自然语言吩咐，或查看[命令参考](docs/4-commands.md)。

---

## Agent 模型基准测试

litman 的 agent 层（内置的 `lit-library` 和 `lit-reading` skill）被设计为可与
你让 Claude Code 接入的任意模型协作，不限于 Anthropic 自家模型。为了解不同模型
驱动它的效果，我们把每个模型作为 Claude Code 后端，让它通过 skill 操作 litman，
覆盖 **22 项日常工作流任务**（添加、阅读、打标签、修改、链接、导出、taxonomy
编辑、健康检查……），每项 3 轮，基于 **litman 1.0.0** 代码（[commit 876d11c](https://github.com/wqx1999/litman/commit/876d11c)，2026 年 6 月）。

**分数的含义。** 每项任务是一个**干净上下文中的单轮 prompt**：一个全新的 agent
收到一条自然语言指令，必须在这一轮内完成它，没有先前对话、没有后续追问。**TRR**
（任务完成率）是最终 vault 状态通过的任务比例；**RA**（路由准确率）是 agent
为请求选对 skill 的频率。

**低分不代表模型不能操作 litman。** 它只是表示模型从冷启动**一次性**完成任务的
频率更低。给予更多引导（更详细的请求，或几轮追问），低分模型同样能完成相同的
工作。这是一个刻意设置的高难度零样本下限，而非上限。

| 模型 | 任务完成率 (TRR) | 路由准确率 (RA) |
|:---|---:|---:|
| [Claude Sonnet 4.6](https://www.anthropic.com) | 97% | 100% |
| [Claude Haiku 4.5](https://www.anthropic.com) | 97% | 79% |
| [DeepSeek-V4 Flash](https://www.deepseek.com) | 80% | 71% |
| [DeepSeek-V4 Pro](https://www.deepseek.com) | 76% | 57% |
| [MiniMax-M3](https://www.minimax.io) | 71% | 75% |
| [GLM-5.1](https://z.ai/model-api) | 58% | 64% |
| [MiMo-V2.5 Pro](https://mimo.mi.com/) | 26% | 0% |
| [MiMo-V2.5](https://mimo.mi.com/) | 21% | 0% |

TRR 是 22 项自动评分任务在 3 轮上的均值；依赖网络的和多轮的场景（代码克隆、
云同步、一个多轮恢复案例）不计入这个单轮分数。无论模型得分如何，数据层都会校验
每一次写入——错误的命令会响亮地失败，而不是把坏数据写进 vault，所以低分模型
只是需要更多轮次，绝不会损坏库。

---

## 文档

完整文档在 [`docs/`](docs/) 下。初次使用 litman？
[教程](docs/5-tutorial.md)覆盖约 80% 的日常使用；其余内容，问 agent 或查命令
参考。[docs/0-readme.md](docs/0-readme.md) 给出整套文档的地图。

| 主题 | 文件 |
|---|---|
| 从这里开始——文档地图 | [docs/0-readme.md](docs/0-readme.md) |
| 设计哲学 | [docs/1-philosophy.md](docs/1-philosophy.md) |
| 四层架构 | [docs/2-architecture.md](docs/2-architecture.md) |
| 概念与字段参考（`metadata.yaml`、`lit-config.yaml`、`TAXONOMY.md`） | [docs/3-concepts.md](docs/3-concepts.md) |
| 命令参考 | [docs/4-commands.md](docs/4-commands.md) |
| 教程 | [docs/5-tutorial.md](docs/5-tutorial.md) |

在本地以静态站点预览文档：

```bash
pip install mkdocs mkdocs-material
mkdocs serve
```

## 致谢

本工具开发于柏林工业大学
[Süssmuth 实验室](https://www.tu.berlin/en/biochemie/research/research-in-suessmuth-group)。
开发过程中使用了
[柏林工业大学 HPC 集群](https://www.tu.berlin/en/hpc-cluster/introduction-slurm-version)。

本项目借助 AI 驱动的开发工具构建：

[![Claude Code](https://img.shields.io/badge/Claude_Code-Anthropic-d4a574?logo=anthropic&logoColor=white)](https://claude.ai/code)
[![Cursor](https://img.shields.io/badge/Cursor-AI_Editor-000000?logo=cursor&logoColor=white)](https://cursor.sh)

让 litman 得以实现的核心依赖：

[![Click](https://img.shields.io/badge/Click-CLI_Framework-4B8BBE?logoColor=white)](https://click.palletsprojects.com/)
[![ruamel.yaml](https://img.shields.io/badge/ruamel.yaml-YAML_Parser-FFDD54?logoColor=black)](https://pypi.org/project/ruamel.yaml/)
[![pypdf](https://img.shields.io/badge/pypdf-PDF_Extraction-EE4C2C?logoColor=white)](https://pypdf.readthedocs.io/)
[![Pydantic](https://img.shields.io/badge/Pydantic-Data_Validation-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Rich](https://img.shields.io/badge/Rich-Terminal_UI-FAD000?logoColor=black)](https://rich.readthedocs.io/)
[![httpx](https://img.shields.io/badge/httpx-HTTP_Client-2D9CDB?logoColor=white)](https://www.python-httpx.org/)

云同步（`lit sync`）由 [rclone](https://rclone.org/) 驱动，这个外部 CLI 把 vault
镜像到它支持的任意云后端——这是 vault 备份和跨机器迁移的主干：

[![rclone](https://img.shields.io/badge/rclone-Cloud_Sync_Engine-3F87E5?logo=rclone&logoColor=white)](https://rclone.org/)

litman 字标改编自 [Nunito](https://fonts.google.com/specimen/Nunito)——由 Vernon Adams、
Cyreal、Jacques Le Bailly 设计——把字形转为轮廓，并加了一点前倾。

## 许可证

MIT。见 [`LICENSE`](LICENSE)。

---

<sub>AI agent：本项目精简、链接密集的导航地图见 [README-Agent.md](README-Agent.md)。</sub>
