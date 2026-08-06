# Security Policy

## Reporting a vulnerability

Email <contact@litman.dev>. Please do not open a public issue for a security
problem — an issue is visible to everyone the moment it is filed.

Include what you did, what happened, and the version (`lit --version`). litman is
a one-person project, so allow a few days for a reply.

## What is worth reporting

litman runs on your own machine and keeps your library in plain files on your own
disk. The places worth looking at are where it crosses that boundary:

- the local Web UI server (`lit gui`) and its HTTP API
- metadata fetched over the network (Crossref) and the PyPI update check
- PDF parsing — the files come from publishers, not from you
- cloud sync through rclone

A crash or a wrong result on its own is a bug, not a vulnerability; those belong
in [issues](https://github.com/wqx1999/litman/issues).

## Supported versions

The latest release on PyPI. Fixes ship as a new release rather than as patches to
older ones.
