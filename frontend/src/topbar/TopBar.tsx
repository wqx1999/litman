import type { VaultsPayload } from '../types'
import logoUrl from '../assets/litman-logo.png'

interface Props {
  vaults: VaultsPayload | null
  search: string
  onSearch: (q: string) => void
}

/** Global chrome: brand, current-vault indicator, and the title/id search. No
 * per-paper or mutating actions live here — copy-id / copy-wikilink moved to the
 * Cockpit (selected-paper context); refresh is gone (browser reload / Phase 3
 * write handlers cover it); project creation lands next to the project dropdown
 * in Phase 3. */
export default function TopBar({ vaults, search, onSearch }: Props) {
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
    </header>
  )
}
