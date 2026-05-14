"""Exporters — project vault out to derived formats (M12).

Mirror of ``litman.importers`` but in the other direction: importers read
external sources (CrossRef, LLM JSON) into the metadata.yaml shape, while
exporters take a metadata.yaml dict and emit a downstream artefact such
as a .bib file. Both layers stay pure / no-I/O so the CLI command can
own filesystem concerns (path resolution, sentinel, atomic write).
"""
