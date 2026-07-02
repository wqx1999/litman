/** Confirm switching the active vault (3c-2).
 *
 * Switching is GLOBAL — it changes the registry's active vault, so subsequent
 * `lit` commands in every terminal without an explicit --library / $LIT_LIBRARY
 * resolve to the new vault, not just this GUI — and it closes every open tab (the
 * old vault's papers don't exist in the new one). Both are consequential, so the
 * switch sits behind a default-Cancel confirm. Any unsaved annotations / notes
 * are flushed before the switch; the body spells that out when dirtyCount > 0.
 * macOS-style modal shell shared with the rest of the app. Not destructive (the
 * switch is reversible), so the confirm button is accent, not rose.
 */
export default function SwitchVaultDialog({
  targetName,
  tabCount,
  dirtyCount,
  switching,
  onCancel,
  onConfirm,
}: {
  targetName: string
  tabCount: number
  dirtyCount: number
  switching: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      onClick={switching ? undefined : onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          Switch vault to “{targetName}”?
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          This changes the active vault{' '}
          <span className="font-medium">globally</span> — it affects{' '}
          <code className="rounded bg-stone-100 px-1 py-0.5">lit</code> in every
          terminal without an explicit{' '}
          <code className="rounded bg-stone-100 px-1 py-0.5">--library</code> /{' '}
          <code className="rounded bg-stone-100 px-1 py-0.5">$LIT_LIBRARY</code>.
          {tabCount > 0 &&
            ` All ${tabCount} open tab${tabCount === 1 ? '' : 's'} will close.`}
        </p>
        {dirtyCount > 0 && (
          <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs leading-relaxed text-amber-700">
            {dirtyCount} tab{dirtyCount === 1 ? '' : 's'} ha
            {dirtyCount === 1 ? 's' : 've'} unsaved changes — they will be saved
            before switching.
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={switching}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={switching}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {switching ? 'Switching…' : dirtyCount > 0 ? 'Save & switch' : 'Switch'}
          </button>
        </div>
      </div>
    </div>
  )
}
