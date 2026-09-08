# Contributing

litman is written and maintained by one person. That shapes everything on this
page: the most useful thing you can do is tell me what broke, and a small change
is much easier to accept than a large one.

## Getting help

Start with the [documentation](https://litman.dev/docs/) — it covers every
command, the layout of a vault, and a tutorial that builds one from scratch.
`lit help` lists the commands from the terminal, and `lit <command> --help`
explains any one of them.

If that does not answer it, open an
[issue](https://github.com/wqx1999/litman/issues) and ask. A question is a fine
reason to open one; if you were confused, the documentation is probably at
fault. Without a GitHub account, or for something you would rather not post in
public, email <contact@litman.dev>.

## Reporting a problem

Open an [issue](https://github.com/wqx1999/litman/issues). The bug report form
asks for your operating system, how you installed litman, and the output of
`lit --version`. Those three answers resolve most reports, so please fill them
in even when the problem seems obvious.

Two things help more than anything else:

- The full error output, not just the last line.
- The steps that produced it, starting from a command you ran.

If the problem is about your data rather than a crash — papers, fields, or
projects looking wrong — include the output of `lit health-check`.

Security problems do not go in an issue. Email <contact@litman.dev> instead;
see [SECURITY.md](SECURITY.md).

## Suggesting a change

Open an issue and describe the problem you hit before describing the feature you
want. Knowing what you were trying to do often leads somewhere different from
the proposal, and it is a cheaper conversation to have before either of us
writes code.

litman is deliberately narrow. It keeps a library of papers you have read, and
it is built so that an AI assistant can work in that library safely. Proposals
that fit that shape have a good chance; proposals that turn it into a general
research platform will probably be declined, and it is nothing personal.

## Sending code

Small, focused patches are welcome. There is no review rota and no team, so
please open an issue first for anything beyond an obvious fix — it is
disappointing for both of us if you write a large change that does not fit.

To set up a working copy:

```bash
git clone https://github.com/wqx1999/litman
cd litman
pip install -e ".[dev]"
pytest -q --ignore=tests/bench
```

litman requires Python 3.12 or newer. Continuous integration runs the same test
command on Linux, macOS and Windows, so a change that assumes one platform will
fail there — be careful with paths, file locking, text encoding, and anything
that depends on a file's modification time.

A few things that make a patch easy to accept:

- Add a test. The suite is large and it is what makes changes safe.
- Keep the change to one thing.
- Match the surrounding code rather than the style you prefer.
- Run `ruff check .` and `mypy` before you push. Both are configured in
  `pyproject.toml`, so they need no arguments.

litman is licensed under AGPL-3.0-or-later. By contributing you agree that your
contribution is licensed the same way.

## What to expect

One maintainer, working on this alongside a PhD. There is no response time to
promise. Issues get read; allow a few days for a reply, and longer for anything
that needs a release. Bug reports about data loss or a broken install jump the
queue.

Development happens in the open in this repository. Releases are tagged and
listed in [CHANGELOG.md](CHANGELOG.md).
