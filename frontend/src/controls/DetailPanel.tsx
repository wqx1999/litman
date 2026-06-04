// DetailPanel: the read-only card shown when a paper node is clicked. It shows
// what the colour/cluster view cannot: the paper's membership across ALL five
// dimensions at once (clickable -> zoom into that slice), its relations in both
// directions (clickable -> select that paper), the bibliographic head, and the
// drift reason if it is red. The GUI is offline & self-contained, so the card
// shows only the projected `meta` fields — the full record is `lit show <id>`.

import { useMemo, type ReactNode } from 'react'
import { type NodeRelations, relationsOf } from '../graph/dimensions'
import { colorForKey } from '../graph/encoding'
import {
  DIMENSIONS,
  DIMENSION_LABEL,
  type Dimension,
  type GraphEdge,
  type GraphNode,
  type NodeMeta,
} from '../types'

interface Props {
  node: GraphNode
  // All nodes, for resolving a relation target id to its title (and to tell a
  // dangling / cross-vault id apart from a real paper).
  nodesById: Map<string, GraphNode>
  // All relation edges (not the focus-filtered set) so the card shows the
  // paper's complete relations regardless of the current view.
  edges: GraphEdge[]
  // Per-dimension drift value sets, to flag a chip whose value is invalid.
  dimensionInvalid: Record<Dimension, string[]>
  onClose: () => void
  onFocusValue: (dim: Dimension, value: string) => void
  onSelectNode: (id: string) => void
}

function authorLine(meta: NodeMeta): string {
  const a = meta.authors
  if (a.length === 0) return ''
  if (meta.n_authors <= 3) return a.slice(0, 3).join('; ')
  return `${a[0]} et al.`
}

const REL_LABEL: Record<keyof NodeRelations, string> = {
  extends: 'Extends →',
  extendedBy: '← Extended by',
  related: 'Related',
  contradicts: 'Contradicts →',
  contradictedBy: '← Contradicted by',
}
const REL_ORDER: (keyof NodeRelations)[] = [
  'extends',
  'extendedBy',
  'related',
  'contradicts',
  'contradictedBy',
]

function Badge({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-stone-200/70 px-2 py-0.5 text-[10px] font-medium text-stone-600">
      {children}
    </span>
  )
}

export function DetailPanel({
  node,
  nodesById,
  edges,
  dimensionInvalid,
  onClose,
  onFocusValue,
  onSelectNode,
}: Props) {
  const rel = useMemo(() => relationsOf(edges, node.id), [edges, node.id])
  const corrupt = node.type === 'corrupt'
  const m = node.meta
  const subtitle = [authorLine(m), m.year ? String(m.year) : '', m.journal]
    .filter(Boolean)
    .join('  ·  ')
  const dimRows = DIMENSIONS.filter((d) => node.dims[d].length > 0)
  const relRows = REL_ORDER.filter((k) => rel[k].length > 0)

  return (
    <div className="rounded-xl border border-[#e3dccd] bg-[#faf8f3]/97 text-stone-700 shadow-lg backdrop-blur">
      <div className="flex items-start gap-2 border-b border-[#e7e1d5] p-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold leading-snug text-stone-800">
            {node.label}
          </div>
          {subtitle && <div className="mt-0.5 text-[11px] text-stone-500">{subtitle}</div>}
          <div className="mt-0.5 font-mono text-[10px] text-stone-400">{node.id}</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="shrink-0 rounded px-1.5 text-stone-400 transition hover:bg-stone-200/60 hover:text-stone-700"
        >
          ✕
        </button>
      </div>

      <div className="max-h-[60vh] overflow-y-auto p-3 text-[12px]">
        {corrupt ? (
          <div className="rounded-md bg-[#f4e3df] px-2.5 py-2 text-[11px] leading-snug text-[#8c3a2c]">
            Unparseable metadata — this paper could not be loaded. Run{' '}
            <span className="font-mono">lit health-check</span> to inspect.
          </div>
        ) : (
          <>
            <div className="mb-2.5 flex flex-wrap gap-1.5">
              {node.status === 'invalid' && (
                <span className="rounded-full bg-[#f4e3df] px-2 py-0.5 text-[10px] font-medium text-[#8c3a2c]">
                  drift
                </span>
              )}
              {m.read_status && <Badge>{m.read_status}</Badge>}
              {m.type && <Badge>{m.type}</Badge>}
              {m.priority && <Badge>priority {m.priority}</Badge>}
            </div>

            {m.doi && (
              <a
                href={`https://doi.org/${m.doi}`}
                target="_blank"
                rel="noreferrer"
                className="mb-2.5 block truncate text-[11px] text-[#4a6b7b] hover:underline"
              >
                doi.org/{m.doi}
              </a>
            )}

            {dimRows.length > 0 && (
              <div className="mb-3 space-y-1.5">
                {dimRows.map((d) => (
                  <div key={d}>
                    <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-stone-400">
                      {DIMENSION_LABEL[d]}
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {node.dims[d].map((v) => {
                        const drift = dimensionInvalid[d].includes(v)
                        return (
                          <button
                            key={v}
                            type="button"
                            onClick={() => onFocusValue(d, v)}
                            title="Zoom into this slice"
                            className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition hover:bg-stone-200/50 ${
                              drift
                                ? 'border-[#d8b6ad] text-[#8c3a2c]'
                                : 'border-[#ddd5c6] text-stone-600'
                            }`}
                          >
                            <span
                              className="inline-block h-2 w-2 rounded-full"
                              style={{ backgroundColor: drift ? '#a8493a' : colorForKey(v) }}
                            />
                            {v}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {relRows.length > 0 ? (
              <div className="space-y-1.5 border-t border-[#e7e1d5] pt-2.5">
                {relRows.map((k) => (
                  <div key={k}>
                    <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-stone-400">
                      {REL_LABEL[k]}
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {rel[k].map((id) => {
                        const target = nodesById.get(id)
                        return (
                          <button
                            key={id}
                            type="button"
                            disabled={!target}
                            onClick={() => target && onSelectNode(id)}
                            title={target?.label ?? id}
                            className={`max-w-[12rem] truncate rounded border px-1.5 py-0.5 text-[11px] transition ${
                              target
                                ? 'border-[#ddd5c6] text-stone-600 hover:bg-stone-200/50'
                                : 'cursor-default border-dashed border-[#d8b6ad] text-stone-400'
                            }`}
                          >
                            {target ? target.label : `${id} (missing)`}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="border-t border-[#e7e1d5] pt-2.5 text-[11px] text-stone-400">
                No relations.
              </div>
            )}
          </>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-[#e7e1d5] px-3 py-2 text-[10px] text-stone-400">
        <span>
          {node.degree} relation{node.degree === 1 ? '' : 's'}
        </span>
        <span className="font-mono">lit show {node.id}</span>
      </div>
    </div>
  )
}
