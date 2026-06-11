import { useEffect, useRef, useState } from 'react'
import type { PaperMeta } from '../types'
import { fetchCite } from '../api'

interface Props {
  paper: PaperMeta | null
  loading: boolean
  collapsed: boolean
  onToggle: () => void
  onOpenPaper: (id: string) => void
  /** Active vault's filesystem path (server-side), for the copy-path action. */
  vaultPath: string | null
}

function Chips({ values }: { values: string[] | undefined }) {
  if (!values || values.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((v) => (
        <span
          key={v}
          className="rounded-md bg-stone-200 px-2 py-0.5 text-xs text-stone-700"
        >
          {v}
        </span>
      ))}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-stone-500">
        {label}
      </div>
      <div className="text-sm text-stone-800">{children}</div>
    </div>
  )
}

function Relations({
  paper,
  onOpenPaper,
}: {
  paper: PaperMeta
  onOpenPaper: (id: string) => void
}) {
  const groups: Array<[string, string[] | undefined]> = [
    ['related', paper.related],
    ['extends', paper.extends],
    ['extended-by', paper['extended-by']],
    ['contradicts', paper.contradicts],
    ['contradicted-by', paper['contradicted-by']],
  ]
  const nonEmpty = groups.filter(([, ids]) => ids && ids.length > 0)
  if (nonEmpty.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="space-y-1">
      {nonEmpty.map(([rel, ids]) => (
        <div key={rel}>
          <span className="text-xs text-stone-500">{rel}: </span>
          {ids!.map((id) => (
            <button
              key={id}
              onClick={() => onOpenPaper(id)}
              className="mr-1 text-xs text-accent-600 transition-colors hover:underline"
            >
              {id}
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}

/** Read-only metadata cockpit (Phase 1 — no write controls, those are Phase 3).
 * Collapses to a narrow strip via an animated width, mirroring BrowsePanel. */
export default function Cockpit({
  paper,
  loading,
  collapsed,
  onToggle,
  onOpenPaper,
  vaultPath,
}: Props) {
  const [copied, setCopied] = useState<string | null>(null)
  // Caveats from the last Cite (unverified abbreviation, missing fields, ...).
  // Persisted until the selected paper changes so the user actually reads them.
  const [citeWarn, setCiteWarn] = useState<string[] | null>(null)

  // The currently-shown paper id, refreshed synchronously each render and read
  // inside the async Cite handler to drop a response that lands after the user
  // has already moved to a different paper.
  const shownId = useRef<string | undefined>(paper?.id)
  shownId.current = paper?.id

  // Selecting a different paper clears stale copy/cite feedback.
  useEffect(() => {
    setCopied(null)
    setCiteWarn(null)
  }, [paper?.id])

  // Per-paper copy actions live here (the selected-paper context), not the top
  // bar. `ID` pastes into CLI commands / metadata / filenames; `path` is the
  // paper-folder path on the vault host — from it you reach pdf / metadata /
  // notes, or hand it straight to a terminal or an agent.
  async function doCopy(form: string, value: string) {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(form)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* no clipboard in some sandboxes — silently ignore */
    }
  }

  // Cite fetches the server-formatted ACS citation (one formatting path,
  // invariant #16), copies the clean text, and surfaces any caveats so a
  // wrong-looking abbreviation is never copied silently.
  async function doCite() {
    if (!paper) return
    const id = paper.id
    try {
      const { text, warnings } = await fetchCite(id)
      // The selection may have moved on while the request was in flight — drop
      // the stale result rather than copy it / flash its warnings under another
      // paper.
      if (shownId.current !== id) return
      await navigator.clipboard.writeText(text)
      setCopied('citation')
      setCiteWarn(warnings.length ? warnings : null)
      setTimeout(() => setCopied(null), 1200)
    } catch {
      /* no clipboard / fetch failure — silently ignore */
    }
  }

  return (
    <div
      className={`relative flex shrink-0 overflow-hidden border-l border-stone-200 bg-stone-100 transition-[width] duration-300 ease-fluid ${
        collapsed ? 'w-9' : 'w-80'
      }`}
    >
      {/* Collapsed strip: just the expand handle, fading in once narrowed. */}
      <div
        className={`absolute inset-0 flex flex-col items-center pt-3 transition-opacity duration-200 ${
          collapsed ? 'opacity-100 delay-150' : 'pointer-events-none opacity-0'
        }`}
      >
        <button
          onClick={onToggle}
          title="Expand metadata"
          className="text-stone-500 transition-colors hover:text-stone-800"
        >
          ‹
        </button>
      </div>

      {/* Full inspector — fixed w-80 so it never reflows while the container
          width animates; cross-fades out when collapsed. */}
      <aside
        className={`h-full w-80 overflow-auto p-4 transition-opacity duration-200 ${
          collapsed ? 'pointer-events-none opacity-0' : 'opacity-100 delay-100'
        }`}
      >
        <div className="mb-3 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-stone-500">
            Metadata
          </span>
          <button
            onClick={onToggle}
            title="Collapse metadata"
            className="text-stone-500 transition-colors hover:text-stone-800"
          >
            ›
          </button>
        </div>

        {loading && <div className="text-sm text-stone-500">Loading…</div>}
        {!loading && !paper && (
          <div className="text-sm text-stone-500">Select a paper.</div>
        )}

        {paper && (
          <div>
            <div className="mb-4">
              <div className="text-[15px] font-semibold leading-snug text-stone-900">
                {paper.title || paper.id}
              </div>
              <div className="mt-0.5 font-mono text-xs text-stone-500">
                {paper.id}
              </div>
              <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                <button
                  onClick={() => doCopy('ID', paper.id)}
                  title="Copy the paper id"
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900"
                >
                  <span className="text-stone-400">⧉</span> Copy ID
                </button>
                <button
                  onClick={() =>
                    vaultPath && doCopy('path', `${vaultPath}/papers/${paper.id}`)
                  }
                  disabled={!vaultPath}
                  title={
                    vaultPath
                      ? 'Copy the paper folder path'
                      : 'Vault path unavailable'
                  }
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                >
                  <span className="text-stone-400">⧉</span> Copy path
                </button>
                <button
                  onClick={doCite}
                  title="Copy an ACS-style citation (journal abbrev. year, volume, pages)"
                  className="flex items-center gap-1 rounded-md border border-stone-300 bg-white px-2.5 py-1 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900"
                >
                  <span className="text-stone-400">❝</span> Cite
                </button>
                {copied && (
                  <span className="text-[11px] font-medium text-emerald-600">
                    ✓ copied {copied}
                  </span>
                )}
              </div>
              {citeWarn && (
                <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
                  <span className="font-semibold">Citation copied — verify:</span>
                  <ul className="mt-0.5 list-disc space-y-0.5 pl-4">
                    {citeWarn.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
            <Field label="Authors">
              {paper.authors && paper.authors.length > 0
                ? paper.authors.join('; ')
                : '—'}
            </Field>
            <Field label="Venue / Year">
              {[paper.journal, paper.year].filter(Boolean).join(' · ') || '—'}
            </Field>
            {paper.doi && (
              <Field label="DOI">
                <a
                  href={`https://doi.org/${paper.doi}`}
                  target="_blank"
                  rel="noreferrer"
                  className="text-accent-600 transition-colors hover:underline"
                >
                  {paper.doi}
                </a>
              </Field>
            )}
            <Field label="Status / Priority / Type">
              {[paper.status, paper.priority, paper.type]
                .map((v) => v || '—')
                .join(' · ')}
            </Field>
            <Field label="Read-date / Last-revisited">
              {[paper['read-date'], paper['last-revisited']]
                .map((v) => v || '—')
                .join(' · ')}
            </Field>
            <Field label="Topics">
              <Chips values={paper.topics} />
            </Field>
            <Field label="Methods">
              <Chips values={paper.methods} />
            </Field>
            <Field label="Data">
              <Chips values={paper.data} />
            </Field>
            <Field label="Projects">
              <Chips values={paper.projects} />
            </Field>
            <Field label="Relations">
              <Relations paper={paper} onOpenPaper={onOpenPaper} />
            </Field>
            <Field label="Code-clones">
              <Chips values={paper['code-clones']} />
            </Field>
          </div>
        )}
      </aside>
    </div>
  )
}
