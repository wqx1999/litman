# litman documentation

User-facing reference for the `lit` CLI. The top-level
[`../README.md`](../README.md) covers what litman is and how to install
it; this directory holds the long-form reference.

## Contents

| Topic | File |
|---|---|
| Vocabulary and concepts | [concepts.md](concepts.md) |
| Command reference (by scenario) | [commands.md](commands.md) |
| `metadata.yaml` schema | [metadata-schema.md](metadata-schema.md) |
| `lit-config.yaml` schema | [config-schema.md](config-schema.md) |
| `TAXONOMY.md` schema | [taxonomy-schema.md](taxonomy-schema.md) |
| Four-layer architecture | [architecture.md](architecture.md) |
| Design philosophy | [philosophy.md](philosophy.md) |
| Roadmap and release history | [roadmap.md](roadmap.md) |

## Local preview as a static site

```bash
pip install mkdocs mkdocs-material
cd litman/
mkdocs serve
```

Open <http://127.0.0.1:8000> in your browser. Hosting on a public docs
URL is on the roadmap, gated on the PyPI release.
