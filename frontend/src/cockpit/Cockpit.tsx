import type { PaperMeta } from '../types'

interface Props {
  paper: PaperMeta | null
  loading: boolean
  collapsed: boolean
  onToggle: () => void
  onOpenPaper: (id: string) => void
}

function Chips({ values }: { values: string[] | undefined }) {
  if (!values || values.length === 0) return <span className="text-stone-400">—</span>
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((v) => (
        <span
          key={v}
          className="rounded bg-stone-200 px-1.5 py-0.5 text-xs text-stone-700"
        >
          {v}
        </span>
      ))}
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-stone-500">
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
              className="mr-1 text-xs text-stone-700 underline decoration-stone-400"
            >
              {id}
            </button>
          ))}
        </div>
      ))}
    </div>
  )
}

/** Read-only metadata cockpit (Phase 1 — no write controls, those are Phase 3). */
export default function Cockpit({
  paper,
  loading,
  collapsed,
  onToggle,
  onOpenPaper,
}: Props) {
  if (collapsed) {
    return (
      <div className="flex w-8 flex-col items-center border-l border-stone-200 bg-stone-100 pt-3">
        <button
          onClick={onToggle}
          title="Expand metadata"
          className="text-stone-500 hover:text-stone-800"
        >
          ‹
        </button>
      </div>
    )
  }

  return (
    <aside className="w-80 shrink-0 overflow-auto border-l border-stone-200 bg-stone-100 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-stone-500">
          Metadata
        </span>
        <button
          onClick={onToggle}
          title="Collapse metadata"
          className="text-stone-500 hover:text-stone-800"
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
          <Field label="Title">{paper.title || paper.id}</Field>
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
                className="text-stone-700 underline decoration-stone-400"
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
  )
}
