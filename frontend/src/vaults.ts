import type { VaultsPayload } from './types'
import { basename } from './ui/path'

/** The vault this server is bound to, as the UI names it: its registry name,
 * or — opened by path and not registered (`lit gui --library`, $LIT_LIBRARY) —
 * its folder name, with `registered` false. Null while nothing is served.
 * Not the registry's active vault: `lit gui --vault` / `--library` serve
 * another, and naming the active one then labels the wrong library. */
export function servedVault(
  vaults: VaultsPayload | null,
): { name: string; registered: boolean } | null {
  if (!vaults?.served) return null
  const entry = vaults.vaults.find((v) => v.served)
  if (entry) return { name: entry.name, registered: true }
  return { name: basename(vaults.served) || vaults.served, registered: false }
}
