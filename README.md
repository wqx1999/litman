<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo-hero-dark.png"/>
  <img src="https://raw.githubusercontent.com/wqx1999/litman/main/assets/logo-hero.png" width="52%" alt="litman"/>
</picture>

<p>
<a href="https://litman.dev"><img src="https://img.shields.io/badge/website-litman-D97757?logo=github&logoColor=white" alt="Website"/></a>
<a href="https://pypi.org/project/litman/"><img src="https://img.shields.io/pypi/v/litman?logo=pypi&logoColor=white" alt="PyPI version"/></a>
<img src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white" alt="Python 3.12+"/>
<img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/>
<img src="https://img.shields.io/badge/AI--native-agentic%20tool-D97757" alt="AI-native: agentic tool"/>
</p>

</div>

**Keep every paper you read on your own computer, and let an AI assistant do the
filing.**

Open a paper, highlight it, write your notes beside it — everything stays in
ordinary files and folders that you can read, copy, and back up yourself, with or
without litman. For anything past reading, just say what you want — *add this
paper*, *which ones did I read for the peptide project?* — and your AI assistant
does it.

---

## Install

**macOS / Linux:**

```bash
curl -LsSf https://get.litman.dev/install.sh | sh
```

**Windows** (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://get.litman.dev/install.ps1 | iex"
```

Double-click the **litman** icon the installer creates — desktop on Windows,
Launchpad on macOS, applications menu on Linux.

<details>
<summary>Install with pipx</summary>

[pipx](https://pipx.pypa.io) builds litman's environment from the Python already on
your machine, so it needs **Python 3.12 or newer** on your `PATH`.

```bash
pipx install litman
lit gui --make-shortcut    # the icon — the one-line installer does this step for you
```

</details>

<details>
<summary>Install from a local clone (development)</summary>

```bash
# first install
git clone https://github.com/wqx1999/litman.git
cd litman
pipx install .

# update (pull latest code first)
git pull
pipx install --force .
```

</details>

## The AI assistant

Pick an agent to drive litman through its bundled skills; you bring the model — use
whatever you already pay for. **Start with Claude Code.**

| Agent CLI | Recommended model |
|:---|:---|
| [Claude Code](https://claude.ai/code) | Official subscription |
| [Codex](https://developers.openai.com/codex/cli/) | Official subscription |
| [Antigravity CLI](https://antigravity.google/download#antigravity-cli) | Official subscription |
| [Cursor](https://cursor.com/cli) | Composer 2.5 |
| [OpenCode](https://opencode.ai/) | MiniMax-M3 |

Every write is validated, so a weaker model just needs more turns — it never
corrupts the library. Scores and method:
[agent model benchmark](https://litman.dev/docs/6-agent-benchmark/).

## Update and uninstall

<details>
<summary>Update litman</summary>

When a new release is out, a chip with its version number appears next to the
logo in the Web UI. Click it and choose **Update & restart**: litman closes,
upgrades, and reopens by itself.

From a terminal, `lit self-update` does the same through whichever tool
installed litman, uv or pipx. It prints `current → latest` and asks once.

litman asks PyPI for the newest version number once a day, and mentions it when
yours is older; `lit hello` tells you every time you ask.
`LITMAN_NO_UPDATE_CHECK=1` switches all of it off —
[what it checks](https://litman.dev/docs/4-commands/#lit-self-update).

</details>

<details>
<summary>Uninstall litman</summary>

Two steps, in order, while the `lit` command still exists:

```bash
lit uninstall              # bundled skills, desktop shortcut, shell completion, vault registry, agent preferences, app-window browser profile
uv tool uninstall litman   # or: pipx uninstall litman
```

`lit uninstall` lists what it will delete and asks first — `--dry-run` previews,
`-y` skips the prompt. Your vault (papers, PDFs, notes, annotations) is never
touched by any of this; delete that directory by hand if you want the data gone.

</details>

## Documentation

Full documentation lives at [litman.dev/docs](https://litman.dev/docs/). New to
litman? The [tutorial](https://litman.dev/docs/5-tutorial/) covers about 80% of
everyday use.

- [Start here — docs map](https://litman.dev/docs/0-readme/)
- [Design philosophy](https://litman.dev/docs/1-philosophy/)
- [Four-layer architecture](https://litman.dev/docs/2-architecture/)
- [Concepts and field reference](https://litman.dev/docs/3-concepts/)
- [Command reference](https://litman.dev/docs/4-commands/)
- [Tutorial](https://litman.dev/docs/5-tutorial/)
- [Agent model benchmark](https://litman.dev/docs/6-agent-benchmark/)
- [Changelog](https://litman.dev/docs/changelog/)

## Acknowledgments

Developed in the [Süssmuth Lab](https://www.tu.berlin/en/biochemie/research/research-in-suessmuth-group),
Technische Universität Berlin, with access to the
[TU Berlin HPC cluster](https://www.tu.berlin/en/hpc-cluster/introduction-slurm-version).

Built with:

[![Claude Code](https://img.shields.io/badge/Claude_Code-Anthropic-d4a574?logo=anthropic&logoColor=white)](https://claude.ai/code)
[![Cursor](https://img.shields.io/badge/Cursor-AI_Editor-000000?logo=cursor&logoColor=white)](https://cursor.sh)

Standing on:

[![Click](https://img.shields.io/badge/Click-CLI_Framework-4B8BBE?logoColor=white)](https://click.palletsprojects.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Web_Server-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Uvicorn](https://img.shields.io/badge/Uvicorn-ASGI_Server-499848?logoColor=white)](https://www.uvicorn.org/)
[![ruamel.yaml](https://img.shields.io/badge/ruamel.yaml-YAML_Parser-FFDD54?logoColor=black)](https://pypi.org/project/ruamel.yaml/)
[![pypdf](https://img.shields.io/badge/pypdf-PDF_Extraction-EE4C2C?logoColor=white)](https://pypdf.readthedocs.io/)
[![Pydantic](https://img.shields.io/badge/Pydantic-Data_Validation-E92063?logo=pydantic&logoColor=white)](https://docs.pydantic.dev/)
[![Rich](https://img.shields.io/badge/Rich-Terminal_UI-FAD000?logoColor=black)](https://rich.readthedocs.io/)
[![httpx](https://img.shields.io/badge/httpx-HTTP_Client-2D9CDB?logoColor=white)](https://www.python-httpx.org/)

Cloud sync runs on [rclone](https://rclone.org/), installed separately.

The litman wordmark is adapted from [Nunito](https://fonts.google.com/specimen/Nunito).

## Contact

[Issues](https://github.com/wqx1999/litman/issues) are the fastest route for a bug
or a feature request. For anything else, or if you are not on GitHub:
<contact@litman.dev>. Security problems go to the same address, privately — see
[SECURITY.md](https://github.com/wqx1999/litman/blob/main/SECURITY.md).

## License

MIT. See [`LICENSE`](https://litman.dev/docs/license/).

---

<sub>AI agents: a condensed, link-dense map of this project lives in [README-Agent.md](https://github.com/wqx1999/litman/blob/main/README-Agent.md).</sub>
