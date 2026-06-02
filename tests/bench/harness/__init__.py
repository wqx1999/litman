"""litman-bench harness — deterministic core (M34 phases B-seed / B / C / D).

This package is a **read-only consumer** of litman's public APIs. It never
imports from ``litman.commands`` to drive writes — every vault mutation goes
through the ``lit`` CLI as a subprocess (see :mod:`harness.runlit`) so the
isolation env injection is real and the run is logged exactly as an agent's
would be. The only direct ``litman`` imports are the read-side oracle
(:func:`litman.core.checks.run_all_checks`, :func:`litman.core.document.list_papers`)
and small pure helpers (taxonomy parsing, the registry default path).

Phases implemented here (the *deterministic* core, CI-able, no agent / network):

* :mod:`harness.seeds`     — Phase B-seed: deterministic seed-snapshot builder.
* :mod:`harness.runlit`    — Phase B: isolation wrapper + run-vault lifecycle.
* :mod:`harness.checker`   — Phase C: assertion-verb dispatch + ``resolved``.
* :mod:`harness.scenarios` — Phase D: scenario loader + handoff discipline.

Phases E (executor driver), F (routing harness), G (live run) are deferred to
the user's networked machine and are intentionally absent.
"""

from __future__ import annotations
