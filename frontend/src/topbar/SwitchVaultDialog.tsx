import { useT } from '../i18n'
import { rich } from '../i18n/rich'
import { useEscapeLayer } from '../ui/escapeStack'
import { modalBackdropProps } from '../ui/modalShell'

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
  // No `switching` guard, matching the Escape this replaces — the refactor is
  // behaviour-preserving, and the button gating is a separate question.
  useEscapeLayer(true, onCancel)
  const t = useT()
  const s = t.topbar.switchVault
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[24rem] animate-grow-in rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">
          {s.title(targetName)}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-600">
          {rich(s.body(tabCount), {
            globally: <span className="font-medium">{s.globally}</span>,
            lit: <code className="rounded bg-stone-100 px-1 py-0.5">lit</code>,
            library: <code className="rounded bg-stone-100 px-1 py-0.5">--library</code>,
            env: <code className="rounded bg-stone-100 px-1 py-0.5">$LIT_LIBRARY</code>,
          })}
        </p>
        {dirtyCount > 0 && (
          <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs leading-relaxed text-amber-700">
            {s.unsaved(dirtyCount)}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            autoFocus
            onClick={onCancel}
            disabled={switching}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100 disabled:opacity-40"
          >
            {t.common.cancel}
          </button>
          <button
            onClick={onConfirm}
            disabled={switching}
            className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-600 disabled:opacity-60"
          >
            {switching ? s.switching : dirtyCount > 0 ? s.saveAndSwitch : s.switch}
          </button>
        </div>
      </div>
    </div>
  )
}
