import { useState } from 'react'
import type { VaultsPayload } from '../types'
import logoUrl from '../assets/litman-logo.png'

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

/** macOS-toolbar-style button: borderless, hover reveals a soft rounded fill. */
const toolBtn =
  'rounded-md px-2 py-1 text-sm text-stone-600 transition-colors ' +
  'hover:bg-stone-200 active:bg-stone-300 ' +
  'disabled:text-stone-300 disabled:hover:bg-transparent'

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
    <header className="flex items-center gap-2.5 border-b border-stone-200 bg-stone-50/90 px-3 py-2 backdrop-blur-md">
      <img
        src={logoUrl}
        alt="litman"
        title="litman"
        className="h-6 w-auto shrink-0 select-none"
      />

      <select
        value={vaults?.active ?? ''}
        disabled
        title="Vault switching lands in Phase 3"
        className="rounded-md border border-stone-300 bg-stone-100 px-2 py-1 text-sm text-stone-500 shadow-sm"
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
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-stone-400">
          ⌕
        </span>
        <input
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="Search title or id…"
          className="w-full max-w-md rounded-lg border border-stone-300 bg-white py-1.5 pl-8 pr-3 text-sm text-stone-800 shadow-sm transition placeholder:text-stone-400 focus:border-accent-500 focus:outline-none focus:ring-2 focus:ring-accent-500/25"
        />
      </div>

      <div className="flex items-center gap-1">
        <button
          onClick={() => selectedId && doCopy('id', selectedId)}
          disabled={!selectedId}
          title="Copy paper id"
          className={toolBtn}
        >
          ⧉ id
        </button>
        <button
          onClick={() => selectedId && doCopy('link', `[[${selectedId}]]`)}
          disabled={!selectedId}
          title="Copy [[id]] wikilink"
          className={toolBtn}
        >
          ⧉ [[id]]
        </button>
        {copied && (
          <span className="text-xs text-stone-500">copied {copied}</span>
        )}
        <button onClick={onRefresh} title="Refresh" className={toolBtn}>
          ⟳
        </button>
        <button
          disabled
          title="New project — coming in Phase 3"
          className={toolBtn}
        >
          ＋ project
        </button>
      </div>
    </header>
  )
}
