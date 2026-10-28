import { createPortal } from 'react-dom'
import type {
  ActivityLogEntry,
  HealthIssue,
} from '../types'
import type { AgentStatus } from '../api'
import type { ToastVariant } from '../ui/Toast'
import HelpHint from '../ui/HelpHint'
import { useEscapeLayer } from '../ui/escapeStack'
import {
  modalBackdropProps,
  useModalCardFocus,
} from '../ui/modalShell'

/** Per-severity accent for a health finding — readable in both themes (these
 * hues sit outside the inverted `stone` ramp, so no `dark:` variant is needed).
 * `dot` tints the leading marker, `text` the severity label. */
const SEVERITY_STYLE: Record<
  HealthIssue['severity'],
  { dot: string; text: string }
> = {
  error: { dot: 'bg-rose-500', text: 'text-rose-600' },
  warning: { dot: 'bg-amber-500', text: 'text-amber-600' },
  info: { dot: 'bg-sky-500', text: 'text-sky-600' },
}

const SEVERITY_ORDER: Record<HealthIssue['severity'], number> = {
  error: 0,
  warning: 1,
  info: 2,
}

/** The health-check report panel (D1). Mirrors the ProjectManager shell — a
 * portaled `fixed inset-0` backdrop + an `animate-grow-in` card — so it auto-
 * themes to dark via the inverted `stone` ramp + the global `.dark .bg-white`
 * rule (no manual `dark:` variants). The fetch is owned by TopBar (the count
 * badge must outlive a panel close); this is the presentational view of that
 * result. Groups by `category` (server/registry order preserved) then orders
 * each group's rows error→warning→info. Esc + click-outside close. */
export function HealthPanel({
  issues,
  loading,
  error,
  onRerun,
  onClose,
}: {
  issues: HealthIssue[] | null
  loading: boolean
  error: string | null
  onRerun: () => void
  onClose: () => void
}) {
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening (see useModalCardFocus).
  const cardFocus = useModalCardFocus()
  // Stable, category-first grouping: walk the (registry-ordered) list once so the
  // first-seen category order is preserved, then sort each bucket by severity.
  const groups: Array<[string, HealthIssue[]]> = []
  if (issues) {
    const byCat = new Map<string, HealthIssue[]>()
    for (const issue of issues) {
      const bucket = byCat.get(issue.category)
      if (bucket) bucket.push(issue)
      else {
        const fresh = [issue]
        byCat.set(issue.category, fresh)
        groups.push([issue.category, fresh])
      }
    }
    for (const [, bucket] of groups) {
      bucket.sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity])
    }
  }

  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-[34rem] animate-grow-in focus:outline-none flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-stone-900">Health check</h2>
          <button
            type="button"
            disabled={loading}
            onClick={onRerun}
            className="rounded-md px-2 py-0.5 text-[11px] text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40"
          >
            {loading ? 'Running…' : 'Re-run'}
          </button>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Audits library consistency (read-only). Fixes still run from the CLI:
          <span className="font-mono text-stone-600"> lit health-check --fix</span>.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {loading && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              Running checks…
            </div>
          )}
          {!loading && error && (
            <div className="px-3 py-6 text-center text-xs text-rose-600">
              {error}
            </div>
          )}
          {!loading && !error && issues && issues.length === 0 && (
            <div className="px-3 py-6 text-center text-sm font-medium text-emerald-600">
              ✓ No issues
            </div>
          )}
          {!loading &&
            !error &&
            groups.map(([category, bucket]) => (
              <div
                key={category}
                className="border-b border-stone-100 last:border-b-0"
              >
                <div className="bg-stone-50 px-3 py-1.5 font-mono text-[11px] font-semibold uppercase tracking-wider text-stone-600">
                  {category} · {bucket.length}
                </div>
                {bucket.map((issue, i) => (
                  <div
                    key={i}
                    className="flex gap-2 px-3 py-2 last:border-b-0"
                  >
                    <span
                      className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${SEVERITY_STYLE[issue.severity].dot}`}
                    />
                    <div className="min-w-0">
                      <div className="text-sm text-stone-800">{issue.message}</div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px]">
                        <span
                          className={`font-semibold uppercase ${SEVERITY_STYLE[issue.severity].text}`}
                        >
                          {issue.severity}
                        </span>
                        {issue.paper_id && (
                          <span className="font-mono text-stone-400">
                            {issue.paper_id}
                          </span>
                        )}
                      </div>
                      {issue.hint && (
                        <div className="mt-0.5 text-[11px] italic text-stone-400">
                          {issue.hint}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ))}
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** Per-variant glyph for an activity-log row. */
const LOG_GLYPH: Record<ToastVariant, { ch: string; cls: string }> = {
  success: { ch: '✓', cls: 'text-emerald-500' },
  error: { ch: '✗', cls: 'text-rose-500' },
  warning: { ch: '⚠', cls: 'text-amber-500' },
  info: { ch: 'ℹ', cls: 'text-sky-500' },
}

function _logTime(ts: number): string {
  return new Date(ts).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

/** The session activity-log panel (D2). Same portaled, auto-theming shell as
 * HealthPanel / ProjectManager. Renders the buffer newest-first with a per-
 * variant glyph + local HH:MM:SS time. "No activity yet" when empty. Esc +
 * click-outside close. */
export function ActivityLogPanel({
  entries,
  onClose,
}: {
  entries: ActivityLogEntry[]
  onClose: () => void
}) {
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening (see useModalCardFocus).
  const cardFocus = useModalCardFocus()
  // Newest-first without mutating the source buffer.
  const ordered = entries.slice().reverse()
  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[70vh] w-[30rem] animate-grow-in focus:outline-none flex-col rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        <h2 className="text-sm font-semibold text-stone-900">Activity log</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          Recent actions this session (newest first). Cleared on refresh.
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {ordered.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              No activity yet.
            </div>
          )}
          {ordered.map((e, i) => {
            const glyph = LOG_GLYPH[e.variant]
            return (
              <div
                key={i}
                className="flex items-start gap-2 border-b border-stone-100 px-3 py-2 last:border-b-0"
              >
                <span className={`mt-px shrink-0 text-sm ${glyph.cls}`}>
                  {glyph.ch}
                </span>
                <span className="shrink-0 font-mono text-[11px] tabular-nums text-stone-400">
                  {_logTime(e.ts)}
                </span>
                <span className="min-w-0 text-sm text-stone-700">{e.message}</span>
              </div>
            )
          })}
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** The agent overlay's two shapes: the setup/management flow (install a skill,
 * choose a default, or launch once), or the `copy` box when the server cannot
 * pop a terminal window. Both are one `AgentUi`, so
 * the single `onAgentOpenChange(agentUi !== null)` effect keeps every variant
 * inside App's modal-guard (⌥ shortcuts can't punch through). */
export type AgentUi =
  | { kind: 'setup'; status: AgentStatus }
  | { kind: 'copy'; agent: string; command: string }

export function AgentPanel({
  ui,
  busy,
  onPick,
  onInstallSkill,
  onChooseAgent,
  onRecheck,
  onClose,
  notify,
}: {
  ui: AgentUi
  busy: boolean
  onPick: (name: string) => void
  onInstallSkill: (name: string, makeDefault?: boolean) => void
  onChooseAgent: (name: string) => void
  onRecheck: () => void
  onClose: () => void
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening (see useModalCardFocus).
  const cardFocus = useModalCardFocus()
  const copyCommand = (command: string) => {
    navigator.clipboard
      .writeText(command)
      .then(() => notify('Command copied', 'success'))
      .catch(() => notify('Copy failed — select the text and copy it', 'error'))
  }
  useEscapeLayer(true, onClose)
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
      {...modalBackdropProps}
    >
      <div
        {...cardFocus}
        onClick={(e) => e.stopPropagation()}
        className="w-[26rem] animate-grow-in focus:outline-none rounded-2xl bg-white p-5 shadow-xl ring-1 ring-stone-200"
      >
        {ui.kind === 'setup' ? (
          <AgentSetup
            status={ui.status}
            busy={busy}
            onInstallSkill={onInstallSkill}
            onChooseAgent={onChooseAgent}
            onLaunch={onPick}
            onRecheck={onRecheck}
          />
        ) : (
          <>
            <h2 className="text-sm font-semibold text-stone-900">
              Run it in a terminal
            </h2>
            <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
              litman couldn't open a terminal window from here. Open one on the
              machine running litman — if you're connected to a remote server,
              that's the server, not this computer — then start {ui.agent} with:
            </p>
            <div className="mt-3 flex items-center gap-2 rounded-lg border border-stone-200 bg-stone-50 px-3 py-2">
              <code className="min-w-0 flex-1 truncate font-mono text-sm text-stone-800">
                {ui.command}
              </code>
              <button
                type="button"
                onClick={() => copyCommand(ui.command)}
                className="shrink-0 rounded-md border border-stone-300 bg-white px-2 py-1 text-xs font-medium text-stone-600 transition-colors hover:bg-stone-100 hover:text-stone-900"
              >
                Copy
              </button>
            </div>
          </>
        )}
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            Done
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** The onboarding step of the agent panel: a row per catalog agent, each
 * `supported` agent selectable with a state-driven action (get it → install
 * skill → update skill when stale → ready) read off its OWN row's
 * `skill_state` (agents install into different skill locations, so the
 * resolved default's top-level state says nothing about the other rows); any
 * `supported=false` agent renders greyed as a latent "coming soon" placeholder
 * (none ship today — the branch stays for a future roadmap agent).
 * Everything is data-driven off the status payload — no agent name or skill
 * path is hardcoded here (the red-line agent-agnostic frontend).
 * Auto-themes via the inverted stone ramp + `.dark .bg-white`; no `dark:`. */
function AgentSetup({
  status,
  busy,
  onInstallSkill,
  onChooseAgent,
  onLaunch,
  onRecheck,
}: {
  status: AgentStatus
  busy: boolean
  onInstallSkill: (name: string, makeDefault?: boolean) => void
  onChooseAgent: (name: string) => void
  onLaunch: (name: string) => void
  onRecheck: () => void
}) {
  return (
    <>
      <h2 className="text-sm font-semibold text-stone-900">
        {status.default ? 'Manage agents' : 'Set up your agent'}
        <HelpHint text="The toolbar button and ~ open your default agent. A skill also lets it run lit without asking, where the agent allows it." />
      </h2>
      <div className="mt-3 flex flex-col gap-2">
        {status.agents.map((agent) =>
          agent.supported ? (
            <div
              key={agent.name}
              className="rounded-lg border border-stone-200 bg-stone-50 p-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-stone-800">
                    {agent.display}
                  </div>
                  <div className="mt-0.5 text-[11px] leading-none text-stone-500">
                    {agent.brand}
                  </div>
                </div>
                {status.default === agent.name && (
                  <span className="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-700">
                    default
                  </span>
                )}
              </div>
              {!agent.detected ? (
                <div className="mt-2 text-xs leading-relaxed text-stone-500">
                  {agent.kind === 'app'
                    ? `${agent.display} isn't installed, or hasn't been opened yet.`
                    : `${agent.display} isn't installed.`}{' '}
                  <a
                    href={agent.install_url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-medium text-accent-600 transition-colors hover:text-accent-700"
                  >
                    Get {agent.display} →
                  </a>
                </div>
              ) : !agent.skill_state || agent.skill_state === 'absent' ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    onInstallSkill(agent.name, status.default !== agent.name)
                  }
                  className="mt-2 rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-60"
                >
                  {busy
                    ? 'Installing…'
                    : status.default === agent.name
                      ? 'Install skill'
                      : 'Install skill & use'}
                </button>
              ) : agent.skill_state === 'stale' ? (
                /* Installed but out of date with the running litman (server-
                   side content comparison). Same install endpoint — it
                   overwrites the bundled files and keeps the user's own. */
                <div className="mt-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-amber-600">
                    Skill update available
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onLaunch(agent.name)}
                      className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                    >
                      Launch anyway
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onInstallSkill(agent.name)}
                      className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-60"
                    >
                      {busy ? 'Updating…' : 'Update skill'}
                    </button>
                  </div>
                </div>
              ) : (
                <div className="mt-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-medium text-emerald-600">
                    ✓ Ready
                  </span>
                  {status.default !== agent.name ? (
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onLaunch(agent.name)}
                        className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                      >
                        Launch once
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onChooseAgent(agent.name)}
                        className="rounded-lg bg-accent-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition-colors hover:bg-accent-600 disabled:opacity-60"
                      >
                        Set default
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onLaunch(agent.name)}
                      className="rounded-lg border border-stone-300 bg-white px-3 py-1.5 text-xs font-medium text-stone-600 shadow-sm transition-colors hover:bg-stone-50 hover:text-stone-900 disabled:opacity-50"
                    >
                      Launch now
                    </button>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div
              key={agent.name}
              className="flex items-center justify-between rounded-lg border border-stone-200 px-3 py-2.5 opacity-60"
            >
              <div>
                <div className="text-sm font-medium text-stone-400">
                  {agent.display}
                </div>
                <div className="mt-0.5 text-[11px] leading-none text-stone-400">
                  {agent.brand}
                </div>
              </div>
              <span className="text-[10px] uppercase tracking-wide text-stone-400">
                coming soon
              </span>
            </div>
          ),
        )}
      </div>
      {/* Manual fallback for the rare case the focus/visibility auto-recheck
          doesn't fire (e.g. a second monitor where this tab never lost focus). */}
      <button
        type="button"
        disabled={busy}
        onClick={onRecheck}
        className="mt-3 text-xs font-medium text-stone-500 transition-colors hover:text-stone-700 disabled:opacity-50"
      >
        Installed it just now? Recheck
      </button>
    </>
  )
}
