"""Vendored data files shipped inside the litman wheel.

Currently holds ``journal_abbrev.csv`` (the journal full-name -> ISO4
abbreviation table used by ``litman.core.cite``). The file is read via
``importlib.resources.files("litman.data")`` so it resolves the same whether
litman runs from a source checkout or an installed wheel. Keep this an
``__init__.py`` package (not a namespace dir) so ``files(...)`` works and the
``[tool.setuptools.package-data]`` ``litman.data`` pattern bundles the CSV.
"""
