"""Importers fetch metadata from external sources.

Each importer module is a pair of pure-ish functions:

- ``fetch_<source>(identifier, ...)``: makes the network call (or PDF read)
  and returns the raw response.
- ``parse_<source>(raw)``: converts the raw response into the standard
  litman metadata dict::

      {
          "title": str,
          "authors": list[str],          # "Family, Given" each
          "year": int | None,
          "journal": str,
          "doi": str,
          # ... source-specific extras may appear ...
      }

Splitting fetch from parse keeps tests fast (parse is unit-tested with sample
JSON; fetch is integration-tested separately).
"""
