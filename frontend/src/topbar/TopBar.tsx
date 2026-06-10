import { useState } from 'react'
import type { VaultsPayload } from '../types'

interface Props {
  vaults: VaultsPayload | null
  search: string
  onSearch: (q: string) => void
  selectedId: string | null
  onRefresh: () => void
}

/** Copy a string to the clipboard, swallowing failures (no clipboard in some
 * sandboxes); returns whether it succeeded so the UI can flash feedback. */
async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

export default function TopBar({
  vaults,
  search,
  onSearch,
  selectedId,
  onRefresh,
}: Props) {
  const [copied, setCopied] = useState<string | null>(null)

  async function doCopy(form: string, value: string) {
    if (await copy(value)) {
      setCopied(form)
      setTimeout(() => setCopied(null), 1200)
    }
  }

  return (
    <header className="flex items-center gap-3 border-b border-stone-200 bg-stone-50 px-3 py-2">
      <span className="text-sm font-semibold text-stone-800">litman</span>

      <select
        value={vaults?.active ?? ''}
        disabled
        title="Vault switching lands in Phase 3"
        className="rounded border border-stone-300 bg-stone-100 px-2 py-1 text-sm text-stone-600"
      >
        {vaults?.active ? (
          vaults.vaults.map((v) => (
            <option key={v.name} value={v.name}>
              {v.name}
              {v.active ? ' (active)' : ''}
            </option>
          ))
        ) : (
          <option value="">no vault</option>
        )}
      </select>

      <div className="relative flex-1">
        <input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="🔍 Search title or id…"
          className="w-full max-w-md rounded border border-stone-300 bg-white px-2 py-1 text-sm"
        />
      </div>

      <div className="flex items-center gap-1">
        <button
          onClick={() => selectedId && doCopy('id', selectedId)}
          disabled={!selectedId}
          title="Copy paper id"
          className="rounded px-2 py-1 text-sm text-stone-700 hover:bg-stone-200 disabled:text-stone-400"
        >
          ⧉ id
        </button>
        <button
          onClick={() => selectedId && doCopy('link', `[[${selectedId}]]`)}
          disabled={!selectedId}
          title="Copy [[id]] wikilink"
          className="rounded px-2 py-1 text-sm text-stone-700 hover:bg-stone-200 disabled:text-stone-400"
        >
          ⧉ [[id]]
        </button>
        {copied && (
          <span className="text-xs text-stone-500">copied {copied}</span>
        )}
        <button
          onClick={onRefresh}
          title="Refresh"
          className="rounded px-2 py-1 text-sm text-stone-700 hover:bg-stone-200"
        >
          ⟳
        </button>
        <button
          disabled
          title="New project — coming in Phase 3"
          className="rounded px-2 py-1 text-sm text-stone-400"
        >
          ＋ project
        </button>
      </div>
    </header>
  )
}
