<h1>LITerature MANager <img src="assets/logo1.png" width="120" align="right"/></h1>

<br clear="all"/>

<div align="center">

<img src="assets/logo2.svg" width="58%" alt="LITMAN"/>

<p>
<img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
<img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/>
<img src="https://img.shields.io/badge/AI--native-Claude%20Code-D97757?logo=anthropic&logoColor=white" alt="AI-native: Claude Code"/>
</p>

<p><a href="README.md">English</a> | <b>中文</b></p>

</div>

**本地优先、AI 增强的文献管理 CLI。**

一个面向研究论文的本地知识库，以纯文件形式存储在你的磁盘上。论文通过结构化
metadata 和符号链接，显式地与项目、代码仓库以及彼此关联。内置的 Claude Code
skill 让 AI agent 替你操作 CLI；每条命令手动敲入也同样好用。

---

## 使用前须知

几件值得先了解的事：

1. **不要手动移动 vault 或项目文件夹。** 链接（符号链接、项目桥接、registry）
   都是基于路径的，移动会让它们失效。如果确实需要移动，事后运行
   `lit health-check` 找出并修复损坏的部分。
2. **图表阅读需要多模态模型。** 纯文本模型会回退到纯文本抽取（pypdf），看不到
   图或基于图像的表格——没有视觉或 OCR 后端时，不要问它「图/表 N 显示了什么」。
3. **不要手动编辑 metadata。** 用 `lit` 命令修改论文、taxonomy 和配置——
   不确定用哪个命令，就问 AI agent。
4. **Windows 用户。** 基于符号链接的功能（浏览视图、项目桥接）需要管理员权限；
   其余命令不受影响。推荐使用
   [WSL](https://learn.microsoft.com/en-us/windows/wsl/)。

## 核心特性

1. **长期可靠的本地知识库。** 一切都是文件系统上的纯文本——YAML metadata、
   markdown 笔记、原始 PDF。没有云数据库，没有专有容器格式。随处备份，每个
   文件都能当纯文本读，整个库都能 `grep`。

2. **设计上保持一致。** 主题、方法、项目和数据来源由共享的 `TAXONOMY.md`
   受控词典治理。原子操作让交叉引用随库增长保持干净，`lit health-check`
   在漂移累积之前就抓住它。

3. **论文 ↔ 项目 ↔ 代码三角。** 一篇论文可以绑定到多个项目而不重复；每个
   项目获得自己的符号链接工作目录和一份自动生成的 `REFERENCES.md`。每篇论文
   还可以绑定它的官方代码仓库，克隆进 vault 内部。metadata 字段和符号链接共同
   构成一张显式、可导航的知识图——无需手动维护。

4. **AI 原生 CLI。** 两个内置的 Claude Code skill（`lit-library` 负责入库与
   检索，`lit-reading` 负责阅读辅助）教 agent 如何在 vault 中导航并替你操作
   CLI。agent 发出结构化 JSON；CLI 校验并写入数据——即使模型不完美，你的库
   依然正确。

---

## 安装

litman 是一个 Python CLI 工具。用 **pipx** 安装，这样 `lit` 在每个 shell 中
长期可用，且与你其他的 Python 环境隔离。没有 pipx？见
[pipx.pypa.io](https://pipx.pypa.io)。

**从 PyPI 安装**（推荐）：

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

然后运行一次性安装向导：

```bash
lit setup   # 交互式向导：shell 补全 → Claude Code skill → vault 设置 →（可选）云同步
```

## 快速上手

### 通过 AI agent

```
在 /work/me/ 创建一个新的 vault。
把 ~/Downloads/attention_is_all_you_need.pdf 添加到我的 vault。
列出所有打了 topic: transformer 标签的论文。
给 2017_Vaswani_Attention 打上 topic: attention 标签。
把 2017_Vaswani_Attention 链接到项目 MyResearchProject。
把 2017_Vaswani_Attention 从我的 vault 中移除。
```

### CLI

```bash
# 创建 vault（传父目录；CLI 创建子目录并注册它）
lit init /work/me/

# 添加一篇论文
lit add ~/Downloads/attention_is_all_you_need.pdf --doi 10.48550/arXiv.1706.03762

# 浏览
lit list
lit show 2017_Vaswani_Attention

# 打标签
lit taxonomy add topics transformer
lit modify 2017_Vaswani_Attention --add-tag topics=transformer

# 链接到项目
lit link 2017_Vaswani_Attention --project MyResearchProject

# 移除
lit rm 2017_Vaswani_Attention
```


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

| Model | Task completion (TRR) | Routing (RA) |
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

章鱼吉祥物由 [豆包](https://www.doubao.com/) 生成（AI 图像生成）。

## 许可证

MIT。见 [`LICENSE`](LICENSE)。

---

<sub>AI agent：本项目精简、链接密集的导航地图见 [README-Agent.md](README-Agent.md)。</sub>
