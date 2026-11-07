import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  ActivityLogEntry,
  HealthIssue,
} from '../types'
import type { AgentLaunchMode, AgentStatus, AgentStatusEntry } from '../api'
import type { ToastVariant } from '../ui/Toast'
import { enumLabel, formatLocale, getT, useT } from '../i18n'
import HelpHint from '../ui/HelpHint'
import { withCode } from '../ui/codeSpans'
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
 * result. Groups by `category` (server/registry order preserved), headed by
 * the category's name, then orders each group's rows error→warning→info. Only
 * the message shows: the hints name CLI commands, which are the agent's side
 * (`lit health-check` prints them). Esc + click-outside close. */
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
  const t = useT()
  const h = t.topbar.healthPanel
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
          <h2 className="text-sm font-semibold text-stone-900">{h.title}</h2>
          <button
            type="button"
            disabled={loading}
            onClick={onRerun}
            className="rounded-md px-2 py-0.5 text-[11px] text-stone-500 transition-colors hover:bg-stone-100 hover:text-stone-700 disabled:opacity-40"
          >
            {loading ? h.running : h.rerun}
          </button>
        </div>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">{h.intro}</p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {loading && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              {h.runningChecks}
            </div>
          )}
          {!loading && error && (
            <div className="px-3 py-6 text-center text-xs text-rose-600">
              {error}
            </div>
          )}
          {!loading && !error && issues && issues.length === 0 && (
            <div className="px-3 py-6 text-center text-sm font-medium text-emerald-600">
              {h.noIssues}
            </div>
          )}
          {!loading &&
            !error &&
            groups.map(([category, bucket]) => (
              <div
                key={category}
                className="border-b border-stone-100 last:border-b-0"
              >
                <div className="bg-stone-50 px-3 py-1.5 text-[11px] font-semibold tracking-wider text-stone-600">
                  {enumLabel(h.categories, category)} · {bucket.length}
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
                          {enumLabel(h.severity, issue.severity)}
                        </span>
                        {issue.paper_id && (
                          <span className="font-mono text-stone-400">
                            {issue.paper_id}
                          </span>
                        )}
                      </div>
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
            {t.common.done}
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
  return new Date(ts).toLocaleTimeString(formatLocale(), {
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
  const t = useT()
  const l = t.topbar.activityLog
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
        <h2 className="text-sm font-semibold text-stone-900">{l.title}</h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {l.intro}
        </p>
        <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-stone-200">
          {ordered.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-stone-400">
              {l.empty}
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
                <span className="min-w-0 text-sm text-stone-700">{withCode(e.message)}</span>
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
            {t.common.done}
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
  onChooseMode,
  onRecheck,
  onClose,
  notify,
}: {
  ui: AgentUi
  busy: boolean
  onPick: (name: string) => void
  onInstallSkill: (name: string, makeDefault?: boolean) => void
  onChooseAgent: (name: string) => void
  onChooseMode: (name: string, mode: AgentLaunchMode) => Promise<boolean>
  onRecheck: () => void
  onClose: () => void
  notify: (msg: string, variant?: ToastVariant) => void
}) {
  const t = useT()
  const c = t.topbar.agentCopy
  // Focus the card on mount so Escape reaches the handler below — this is a
  // blocking dialog, so nothing else is listening (see useModalCardFocus).
  const cardFocus = useModalCardFocus()
  const copyCommand = (command: string) => {
    navigator.clipboard
      .writeText(command)
      .then(() => notify(getT().topbar.agentCopy.copied, 'success'))
      .catch(() => notify(getT().topbar.agentCopy.copyFailed, 'error'))
  }
  useEscapeLayer(true, onClose)
  if (ui.kind === 'setup') {
    return createPortal(
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 backdrop-blur-sm"
        {...modalBackdropProps}
      >
        <div
          {...cardFocus}
          onClick={(e) => e.stopPropagation()}
          className="flex h-[min(30rem,calc(100vh-4rem))] w-[46rem] max-w-[calc(100vw-2rem)] animate-grow-in overflow-hidden rounded-2xl bg-white shadow-xl ring-1 ring-stone-200 focus:outline-none max-sm:h-[min(38rem,calc(100vh-2rem))] max-sm:flex-col"
        >
          <AgentSetup
            status={ui.status}
            busy={busy}
            onInstallSkill={onInstallSkill}
            onChooseAgent={onChooseAgent}
            onChooseMode={onChooseMode}
            onLaunch={onPick}
            onRecheck={onRecheck}
            onClose={onClose}
          />
        </div>
      </div>,
      document.body,
    )
  }
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
        <h2 className="text-sm font-semibold text-stone-900">
          {c.title}
        </h2>
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">
          {c.body(ui.agent)}
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
            {c.copy}
          </button>
        </div>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            {t.common.done}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/** What an agent's row shows, read off its OWN status entry (agents install
 * into different skill locations, so the default's verdict says nothing about
 * the other rows). `soon` is a `supported=false` placeholder; none ship today. */
type AgentState = 'ready' | 'stale' | 'no-skill' | 'missing' | 'soon'

function agentState(agent: AgentStatusEntry): AgentState {
  if (!agent.supported) return 'soon'
  if (!agent.detected) return 'missing'
  if (!agent.skill_state || agent.skill_state === 'absent') return 'no-skill'
  return agent.skill_state === 'stale' ? 'stale' : 'ready'
}

const AGENT_STATE_DOT: Record<AgentState, string> = {
  ready: 'bg-status-read',
  stale: 'bg-status-skim',
  'no-skill': 'ring-1 ring-inset ring-status-skim',
  missing: 'bg-stone-300',
  soon: 'bg-stone-200',
}

const AGENT_PRIMARY =
  'shrink-0 rounded-full bg-accent-50 px-3 py-1.5 text-xs font-medium text-accent-600 transition-colors duration-200 ease-fluid hover:bg-accent-100 disabled:opacity-50'
const AGENT_SECONDARY =
  'shrink-0 rounded-full px-3 py-1.5 text-xs font-medium text-stone-600 ring-1 ring-inset ring-stone-200 transition-colors duration-200 ease-fluid hover:bg-stone-100 hover:text-stone-900 disabled:opacity-50'

function AgentStateDot({ state }: { state: AgentState }) {
  return (
    <span className={`h-2 w-2 shrink-0 rounded-full ${AGENT_STATE_DOT[state]}`} />
  )
}

/** The App/CLI control. A way that isn't installed stays clickable (that is how
 * its install link is reached), only dimmer. */
function AgentModeControl({
  agent,
  value,
  onChange,
}: {
  agent: AgentStatusEntry
  value: AgentLaunchMode
  onChange: (mode: AgentLaunchMode) => void
}) {
  const t = useT()
  const options: Array<[AgentLaunchMode, string]> = [
    ['app', t.topbar.agentSetup.modeApp],
    ['terminal', t.topbar.agentSetup.modeCli],
  ]
  const moveWith = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const next = e.key === 'ArrowLeft' ? 'app' : 'terminal'
    onChange(next)
    e.currentTarget.querySelector<HTMLElement>(`[data-mode=${next}]`)?.focus()
  }
  return (
    <div
      role="radiogroup"
      aria-label={t.topbar.agentSetup.launchWith}
      onKeyDown={moveWith}
      className="relative grid w-28 shrink-0 grid-cols-2 rounded-lg bg-stone-200/70 p-0.5 dark:bg-stone-100"
    >
      <span
        className={`absolute inset-y-0.5 left-0.5 w-[calc(50%-2px)] rounded-md bg-white shadow-sm transition-transform duration-300 ease-fluid dark:bg-stone-300! ${
          value === 'terminal' ? 'translate-x-full' : ''
        }`}
      />
      {options.map(([mode, label]) => {
        const usable = agent.modes.find((m) => m.mode === mode)?.usable
        return (
          <button
            key={mode}
            type="button"
            role="radio"
            data-mode={mode}
            aria-checked={value === mode}
            tabIndex={value === mode ? 0 : -1}
            onClick={() => onChange(mode)}
            className={`relative rounded-md py-1 text-xs font-medium transition-colors duration-200 ease-fluid ${
              value === mode
                ? 'text-stone-900'
                : usable
                  ? 'text-stone-600 hover:text-stone-800'
                  : 'text-stone-500 hover:text-stone-700'
            }`}
          >
            {label}
          </button>
        )
      })}
    </div>
  )
}

/** One settings line in the detail pane. */
function AgentDetailRow({
  label,
  children,
}: {
  label: string
  children?: React.ReactNode
}) {
  return (
    <div className="flex min-h-12 items-center justify-between gap-4 border-b border-stone-200 px-3.5 py-2 last:border-b-0">
      <span className="shrink-0 text-[13px] text-stone-700">{label}</span>
      <div className="flex min-w-0 items-center gap-2 text-right text-xs text-stone-500">
        {children}
      </div>
    </div>
  )
}

/** The agent panel's setup/management view: agents on the left, the selected
 * one's detail on the right, a line per thing to see or do. Later choices only
 * add lines, and the card keeps its size however many agents there are; only
 * the list scrolls.
 *
 * `picked` holds a click on a way that isn't installed: the control shows it and
 * the main action becomes its install link, but nothing is saved, or the server
 * would hand back the way that works. It is saved the moment a re-read finds it
 * installed, which is what the focus auto-recheck and Recheck are waiting for.
 * Going back to the way the server would take anyway saves nothing, so looking
 * at the other way can't overwrite a saved choice that is only out of reach.
 *
 * Everything is data-driven off the status payload — no agent name or skill path
 * is hardcoded here (the red-line agent-agnostic frontend). Auto-themes via the
 * inverted stone ramp + `.dark .bg-white`; the control's track and thumb are the
 * only colors that need a `dark:`. */
function AgentSetup({
  status,
  busy,
  onInstallSkill,
  onChooseAgent,
  onChooseMode,
  onLaunch,
  onRecheck,
  onClose,
}: {
  status: AgentStatus
  busy: boolean
  onInstallSkill: (name: string, makeDefault?: boolean) => void
  onChooseAgent: (name: string) => void
  onChooseMode: (name: string, mode: AgentLaunchMode) => Promise<boolean>
  onLaunch: (name: string) => void
  onRecheck: () => void
  onClose: () => void
}) {
  const t = useT()
  const s = t.topbar.agentSetup
  const agents = status.agents
  const [selected, setSelected] = useState<string | undefined>(
    () =>
      (agents.find((a) => a.name === status.default) ??
        agents.find((a) => a.detected) ??
        agents[0])?.name,
  )
  const [picked, setPicked] = useState<Record<string, AgentLaunchMode>>({})
  const unpick = (name: string) =>
    setPicked(({ [name]: _gone, ...rest }) => rest)
  // Launch waits for a save still on its way: the server decides by what is on
  // disk, so going first would start the way the user just left.
  const lastSave = useRef<Promise<boolean>>(Promise.resolve(true))
  const save = (name: string, mode: AgentLaunchMode) => {
    lastSave.current = onChooseMode(name, mode)
  }

  useEffect(() => {
    for (const [name, mode] of Object.entries(picked)) {
      const agent = status.agents.find((a) => a.name === name)
      if (agent?.modes.find((m) => m.mode === mode)?.usable) {
        unpick(name)
        if (mode !== agent.mode) save(name, mode)
      }
    }
    // Only a new status can turn a pick usable; a new pick was checked on click.
  }, [status])

  const listRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const move = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return
      e.preventDefault()
      e.stopPropagation()
      const at = agents.findIndex((a) => a.name === selected)
      const next = agents[at + (e.key === 'ArrowDown' ? 1 : -1)]
      if (!next) return
      setSelected(next.name)
      listRef.current
        ?.querySelector<HTMLElement>(`[data-agent="${next.name}"]`)
        ?.focus()
    }
    window.addEventListener('keydown', move, true)
    return () => window.removeEventListener('keydown', move, true)
  }, [agents, selected])

  const agent = agents.find((a) => a.name === selected)
  const state = agent ? agentState(agent) : null
  const isDefault = !!agent && status.default === agent.name
  const mode = agent ? (picked[agent.name] ?? agent.mode) : null
  const way = agent?.modes.find((m) => m.mode === mode)
  const twoWays = (agent?.modes.length ?? 0) > 1

  const chooseMode = (next: AgentLaunchMode) => {
    if (!agent || next === mode) return
    if (next === agent.mode) {
      unpick(agent.name)
    } else if (agent.modes.find((m) => m.mode === next)?.usable) {
      unpick(agent.name)
      save(agent.name, next)
    } else {
      setPicked((p) => ({ ...p, [agent.name]: next }))
    }
  }
  const launch = (name: string) => {
    void lastSave.current.then((saved) => {
      if (saved) onLaunch(name)
    })
  }

  return (
    <>
      <div className="flex w-60 shrink-0 flex-col border-r border-stone-200 bg-stone-100/70 max-sm:max-h-[42%] max-sm:w-full max-sm:border-b max-sm:border-r-0">
        <div className="px-5 pb-3 pt-5 max-sm:pb-2 max-sm:pt-4">
          <h2 className="text-sm font-semibold text-stone-900">
            {status.default ? s.titleManage : s.titleSetup}
            <HelpHint text={s.help} />
          </h2>
        </div>
        <div
          ref={listRef}
          className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2.5"
        >
          {agents.map((a) => {
            const active = a.name === selected
            return (
              <button
                key={a.name}
                type="button"
                data-agent={a.name}
                aria-current={active}
                onClick={() => setSelected(a.name)}
                className={`flex w-full shrink-0 items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors duration-200 ease-fluid focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent-500 ${
                  active ? 'bg-accent-100/70' : 'hover:bg-stone-200/70'
                }`}
              >
                <AgentStateDot state={agentState(a)} />
                <span
                  className={`min-w-0 flex-1 truncate text-[13px] ${
                    active ? 'font-medium text-stone-900' : 'text-stone-700'
                  }`}
                >
                  {a.display}
                </span>
                {status.default === a.name && (
                  <span className="shrink-0 rounded-full bg-stone-200/70 px-1.5 py-0.5 text-[10px] font-medium text-stone-600">
                    {s.defaultBadge}
                  </span>
                )}
              </button>
            )
          })}
        </div>
        {/* Manual fallback for the rare case the focus/visibility auto-recheck
            doesn't fire (e.g. a second monitor where this tab never lost focus). */}
        <div className="px-5 pb-7 pt-3 max-sm:pb-3 max-sm:pt-2">
          <button
            type="button"
            disabled={busy}
            onClick={onRecheck}
            className="text-xs font-medium text-stone-500 transition-colors hover:text-stone-700 disabled:opacity-50"
          >
            {s.recheck}
          </button>
        </div>
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col p-6 max-sm:p-4">
        {agent && state && (
          <div
            key={agent.name}
            className="min-h-0 flex-1 animate-grow-in overflow-y-auto"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="truncate text-base font-semibold text-stone-900">
                  {agent.display}
                </div>
                <div className="mt-1 text-xs leading-none text-stone-500">
                  {agent.brand}
                </div>
              </div>
              {state !== 'soon' && way && !way.usable ? (
                <a
                  href={way.install_url}
                  target="_blank"
                  rel="noreferrer"
                  className={AGENT_PRIMARY}
                >
                  {twoWays
                    ? way.mode === 'app'
                      ? s.getApp
                      : s.getCli
                    : s.getAgent(agent.display)}
                </a>
              ) : state === 'ready' || state === 'stale' ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => launch(agent.name)}
                  className={AGENT_PRIMARY}
                >
                  {s.launch}
                </button>
              ) : null}
            </div>
            <div className="mt-5 rounded-xl border border-stone-200 bg-stone-50">
              {state === 'soon' && <AgentDetailRow label={s.comingSoon} />}
              {state !== 'soon' && twoWays && mode && (
                <AgentDetailRow label={s.launchWith}>
                  {way && !way.usable && <span>{s.notInstalled}</span>}
                  <AgentModeControl
                    agent={agent}
                    value={mode}
                    onChange={chooseMode}
                  />
                </AgentDetailRow>
              )}
              {state === 'missing' && !twoWays && (
                <AgentDetailRow label={s.notInstalled}>
                  {way?.mode === 'app' && s.openOnce}
                </AgentDetailRow>
              )}
              {state !== 'soon' && state !== 'missing' && (
                <AgentDetailRow label={s.skill}>
                  {state === 'ready' && (
                    <span className="flex items-center gap-2">
                      <AgentStateDot state="ready" />
                      {s.upToDate}
                    </span>
                  )}
                  {state === 'stale' && (
                    /* Installed but out of date with the running litman (server-
                       side content comparison). Same install endpoint — it
                       overwrites the bundled files and keeps the user's own. */
                    <>
                      <span>{s.updateAvailable}</span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => onInstallSkill(agent.name)}
                        className={AGENT_PRIMARY}
                      >
                        {busy ? s.updating : s.updateSkill}
                      </button>
                    </>
                  )}
                  {state === 'no-skill' && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onInstallSkill(agent.name, !isDefault)}
                      className={AGENT_PRIMARY}
                    >
                      {busy
                        ? s.installing
                        : isDefault
                          ? s.installSkill
                          : s.installSkillAndUse}
                    </button>
                  )}
                </AgentDetailRow>
              )}
              {state === 'ready' && (
                <AgentDetailRow label={s.defaultAgent}>
                  {isDefault ? (
                    <span>{s.opensWith}</span>
                  ) : (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => onChooseAgent(agent.name)}
                      className={status.default ? AGENT_SECONDARY : AGENT_PRIMARY}
                    >
                      {s.setDefault}
                    </button>
                  )}
                </AgentDetailRow>
              )}
            </div>
          </div>
        )}
        <div className="mt-4 flex shrink-0 justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-xs text-stone-600 transition-colors hover:bg-stone-100"
          >
            {t.common.done}
          </button>
        </div>
      </div>
    </>
  )
}
