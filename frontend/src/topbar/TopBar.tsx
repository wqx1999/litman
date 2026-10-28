import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type {
  ActivityLogEntry,
  HealthIssue,
  IndexPaper,
  ProjectEntry,
  VaultsPayload,
} from '../types'
import type { Candidate } from '../search'
import { servedVault } from '../vaults'
import {
  ApiError,
  fetchAgentStatus,
  fetchHealth,
  installAgentSkill,
  launchAgent,
  postSelfUpdate,
  setDefaultAgent,
} from '../api'
import type { AgentLaunchResult, AgentStatus } from '../api'
import SearchBox from './SearchBox'
import ProjectManager from './ProjectManager'
import VaultManager from './VaultManager'
import { ActivityLogPanel, AgentPanel, HealthPanel } from './Panels'
import type { AgentUi } from './Panels'
import type { ToastVariant } from '../ui/Toast'
import LitmanMark from '../ui/LitmanMark'
import {
  FolderIcon,
  IconAgent,
  IconFocus,
  IconHelp,
  IconLog,
  IconMoon,
  IconRefresh,
  IconShield,
  IconSun,
  IconVault,
} from '../ui/icons'

interface Props {
  vaults: VaultsPayload | null
  /** The newer litman version available on PyPI (null = up to date / unknown).
   * From the read-only /api/version cache; drives the update chip by the logo. */
  updateAvailable?: string | null
  /** The running litman version, from the same /api/version read — the update
   * chip's popover shows "You have X". */
  currentVersion?: string | null
  /** Registered projects backing the global Projects manager (P4). */
  projects: ProjectEntry[]
  /** Full INDEX projection — backs the delete-project confirm's "N papers" count. */
  allPapers: IndexPaper[]
  search: string
  onSearch: (q: string) => void
  /** Ranked typeahead candidates (App merges client id/title + server md hits). */
  searchCandidates: Candidate[]
  /** A server notes/discussion search is in flight. */
  searchLoading: boolean
  /** Open the scope picked from the dropdown (PDF for id/title, the notes /
   * discussion doc scrolled to the match for a markdown hit). */
  onSelectResult: (candidate: Candidate) => void
  focusMode: boolean
  onToggleFocus: () => void
  /** Force an immediate resync-from-disk (manual refresh button) so changes made
   * outside the GUI — a CLI command, an agent writing notes — surface on demand.
   * The focus-driven sweep does this automatically; this is the explicit path. */
  onRefresh: () => void
  /** Refresh the registered-project list + papers after a create/delete (P4). */
  onProjectsChanged: () => void
  /** Switch the active vault (3c-2) — App handles the confirm + re-fetch. */
  onSwitchVault: (name: string) => void
  /** Register an EXISTING vault dir (vault-manager slice). Rejects with the
   * backend's verbatim VaultRegistryError so the form can show it inline. On
   * success App refreshes the list and, when `setActive`, reuses switchVault. */
  onRegisterVault: (name: string, path: string, setActive: boolean) => Promise<void>
  /** Create a NEW vault dir under `parentDir` and register it (the `lit init`
   * backend). Same contract as onRegisterVault: rejects with the backend's
   * verbatim message so the form can show it inline. */
  onCreateVault: (parentDir: string, name: string, setActive: boolean) => Promise<void>
  /** Unregister a vault (registry entry only — the directory on disk is kept).
   * App handles the toast + list refresh; errors are toasted, not thrown. */
  onUnregisterVault: (name: string) => Promise<void>
  /** Re-point a moved vault at its new directory (`lit vault set-path`). The
   * move-recovery door: the Locate action on a missing vault row calls this.
   * Rejects with the backend's verbatim VaultRegistryError so the inline path
   * input can show it; on success App resyncs (repoints the server + clears the
   * 410 banner when the relocated vault is the served one). */
  onRelocateVault: (name: string, path: string) => Promise<void>
  /** A vault switch is in flight — disables the selector to block re-entry. */
  switching: boolean
  /** Toast a message (surfaces the backend's raw error verbatim). */
  notify: (msg: string, variant?: ToastVariant) => void
  /** Current theme + toggle, lifted to App (Phase 4) so the `L` shortcut and the
   * header button drive ONE theme state. The toggle still flips the `.dark`
   * class + persists the choice (see App's useDarkMode). */
  dark: boolean
  onToggleDark: () => void
  /** Report the global Projects manager's open state up so the shortcut
   * dispatcher's modal guard suppresses global keys while it is up (Phase 4). */
  onProjectsOpenChange: (open: boolean) => void
  /** Same mirror for the vault-manager panel (+ its nested register form /
   * unregister confirm) — without it the global ⌥-write shortcuts fire behind
   * the open panel (the modal-guard red line). */
  onVaultManagerOpenChange: (open: boolean) => void
  /** Open the keyboard-shortcut cheat sheet (Phase 4). A visible affordance for
   * the otherwise hidden `?` convention — without it the shortcuts are
   * undiscoverable (you can't learn `?` opens them if nothing points at it). */
  onShowShortcuts: () => void
  /** Reopen the "What's new" card (post-update highlights) on demand — the
   * litman mark doubles as its entry point, macOS about-box style. */
  onShowWhatsNew?: () => void
  /** Session activity log (newest last) that the log panel renders newest-first.
   * App owns the buffer so every `notify` auto-records (observability slice). */
  activityLog: ActivityLogEntry[]
  /** A new log entry has arrived since the panel was last opened — lights a dot
   * on the log icon. */
  logUnread: boolean
  /** Clear the unread dot — called when the activity-log panel opens. */
  onLogOpened: () => void
  /** Report whether either observability panel (log / health) is open, so App's
   * shortcut modal-guard suppresses global keys while one is up — mirrors
   * onProjectsOpenChange. Without it ⌥-write shortcuts fire behind the panel. */
  onObservabilityOpenChange: (open: boolean) => void
  /** Same mirror for the agent panel (management / copy box) — the modal-guard
   * red line applies to every TopBar overlay. */
  onAgentOpenChange: (open: boolean) => void
  /** Hand App a stable opener for the agent button so the global `` ` ``
   * shortcut and the button run the SAME code path (launch when configured,
   * onboarding panel when not) — mirrors Cockpit's onRegisterHandle. Called
   * with null on unmount. */
  onRegisterAgentOpen: (open: (() => void) | null) => void
  /** Register the secondary action for the same agent button: open agent
   * management without adding another toolbar icon. App binds it to Ctrl+~;
   * the button itself exposes it through right-click. */
  onRegisterAgentManage: (open: (() => void) | null) => void
  /** Same idiom for the vault manager: hand App a stable opener so the vault-gone
   * banner's button opens the SAME panel the toolbar's vault icon does. Called
   * with null on unmount. */
  onRegisterVaultManagerOpen: (open: (() => void) | null) => void
  /** In trash mode, hide the library-scoped controls (vault switch, Projects,
   * search) — they act on the live library, not the trash being browsed. The
   * vault identity moves into the trash banner (see TrashView). */
  trashMode?: boolean
}

/** Global chrome: brand, current-vault indicator, the global Projects manager,
 * the title/id search, and the focus / dark-mode toggles. No per-paper actions
 * live here — copy-id / cite moved to the Cockpit (selected-paper context), and
 * per-paper project link/unlink stays in the Cockpit's project dropdown. Project
 * register/remove are GLOBAL operations, so they sit in the labeled Projects
 * control here (P4), next to the vault indicator — the workspace-level region
 * that future vault operations will join. */
export default function TopBar({
  vaults,
  updateAvailable,
  currentVersion,
  projects,
  allPapers,
  search,
  onSearch,
  searchCandidates,
  searchLoading,
  onSelectResult,
  focusMode,
  onToggleFocus,
  onRefresh,
  onProjectsChanged,
  onSwitchVault,
  onRegisterVault,
  onCreateVault,
  onUnregisterVault,
  onRelocateVault,
  switching,
  notify,
  dark,
  onToggleDark,
  onProjectsOpenChange,
  onVaultManagerOpenChange,
  onShowShortcuts,
  onShowWhatsNew,
  activityLog,
  logUnread,
  onLogOpened,
  onObservabilityOpenChange,
  onAgentOpenChange,
  onRegisterAgentOpen,
  onRegisterAgentManage,
  onRegisterVaultManagerOpen,
  trashMode,
}: Props) {
  const [showProjects, setShowProjects] = useState(false)
  const [showVaults, setShowVaults] = useState(false)
  const [updateOpen, setUpdateOpen] = useState(false)
  // One-click update lifecycle: 'busy' while POST /api/self-update is in
  // flight, 'closing' once accepted (full-screen overlay; the server is going
  // down and the helper takes over). A 409 lands in updateRefused, whose text
  // replaces the button and carries its own manual instructions — the popover
  // offers one action, never a button and a command to choose between.
  const [updatePhase, setUpdatePhase] = useState<null | 'busy' | 'closing'>(null)
  const [updateRefused, setUpdateRefused] = useState<string | null>(null)

  const startSelfUpdate = () => {
    if (updatePhase) return
    setUpdatePhase('busy')
    postSelfUpdate()
      .then(() => {
        setUpdatePhase('closing')
        // Give the overlay a beat to paint, then try to close the app window
        // (best-effort: a plain browser tab may refuse — the overlay text
        // covers that). The helper relaunches the GUI once the upgrade lands.
        setTimeout(() => window.close(), 800)
      })
      .catch((err) => {
        setUpdatePhase(null)
        setUpdateRefused(
          err instanceof ApiError
            ? err.message
            : 'Could not start the update — run `lit self-update` in a terminal.',
        )
      })
  }
  // Hand the opener up to App (the vault-gone banner drives it). `setShowVaults`
  // is stable, so unlike the agent opener this needs no ref to stay identity-safe.
  useEffect(() => {
    onRegisterVaultManagerOpen(() => setShowVaults(true))
    return () => onRegisterVaultManagerOpen(null)
  }, [onRegisterVaultManagerOpen])
  // Only one of the two observability panels is open at a time; opening either
  // closes the other. Opening the log also clears the unread dot.
  const [panel, setPanel] = useState<null | 'log' | 'health'>(null)
  // Health result lifted above the panel so the shield's count badge survives a
  // panel close. `null` = no result for the CURRENT vault yet → no badge (the
  // effect below resets it on a switch, so it is not only "never run"). Two
  // things fill it: one run deferred to idle after boot (the effect below — a
  // GUI-only user never clicks the shield, so the badge used to be permanently
  // empty for them), and the existing re-run on every panel open. Nothing else
  // re-runs it: not focus, not resync, no timer (run_all_checks is Tier-2).
  const [healthIssues, setHealthIssues] = useState<HealthIssue[] | null>(null)
  const [healthLoading, setHealthLoading] = useState(false)
  const [healthError, setHealthError] = useState<string | null>(null)

  // Two independent knobs — kept separate on purpose, so a future foreground
  // caller can disown a stale run and still be told when it fails:
  //   `isStale` — drop a result whose caller has moved on (the boot run hands
  //     in its `cancelled` flag), so vault A's answer never writes into vault B.
  //   `silent`  — never surface a FAILURE. Only the boot run, which nobody
  //     asked for, sets this. The panel renders `error` INSTEAD of the issue
  //     list, so a background 500 landing on an open panel would wipe out the
  //     report the user is reading. A silent run still fills the badge on
  //     success; its failure leaves the badge as it was.
  // Clearing a stale error at the start of every run is always right, so that
  // one write is ungated. The panel's own calls pass nothing and are unchanged.
  //
  // `.finally` is deliberately gated too, even though that leaves a disowned
  // run's `healthLoading` true: the alternative is worse — a stale run clearing
  // the flag while the NEW vault's run is still in flight, i.e. a panel that
  // says it is done when it is not. The gated version always self-heals,
  // because the only thing that disowns a run (a vault change) is immediately
  // followed by a fresh run that sets the flag and clears it. Do not "fix" it.
  const runHealth = (opts?: { isStale?: () => boolean; silent?: boolean }) => {
    const isStale = opts?.isStale
    if (isStale?.()) return
    setHealthLoading(true)
    setHealthError(null)
    fetchHealth()
      .then((issues) => {
        if (isStale?.()) return
        setHealthIssues(issues)
      })
      .catch((err) => {
        if (opts?.silent || isStale?.()) return
        setHealthError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (isStale?.()) return
        setHealthLoading(false)
      })
  }

  // Boot-time run, deferred: fills the shield badge with no click. Idle-
  // scheduled (plain 300ms timer where requestIdleCallback is missing — Safari
  // only got it in 16.4) so it never competes with first paint. What that
  // guarantees is "past first paint", NOT "after every startup fetch": the
  // {timeout: 1500} fires unconditionally, so on a slow boot the health request
  // can overlap the tail of the startup burst. The timeout stays anyway — on a
  // page that never goes idle, dropping it would mean a badge that never fills,
  // which is the worse failure. Keyed on the SERVED VAULT rather than
  // [] because TopBar is NOT remounted across a vault switch (no `key`, no
  // reload, and App's `served` gate goes truthy → truthy so React reconciles in
  // place): with an empty dep array, vault A's badge would sit on vault B. It
  // is `served`, not `active`, for the reason types.ts gives — `active` is the
  // REGISTRY's active name and is null whenever the served vault was found any
  // other way (`lit gui --library`, $LIT_LIBRARY, cwd-walk), which would leave
  // those users with a permanently empty badge. The cleanup also disowns a
  // check scheduled or in flight when the user switches away.
  //
  // Dropping the previous result BEFORE scheduling is what keeps the badge
  // honest across a switch: otherwise vault A's count sits on vault B for the
  // whole idle wait, and if B's check then fails silently it would sit there
  // for good — the very thing this effect's per-vault key exists to prevent.
  // The error goes with it for the same reason: A's failure message is not
  // about B, and waiting for B's own run to clear it leaves it readable in the
  // panel for the whole idle wait. On first mount both are no-ops (already
  // null).
  useEffect(() => {
    if (!vaults?.served) return
    setHealthIssues(null)
    setHealthError(null)
    let cancelled = false
    const go = () => runHealth({ isStale: () => cancelled, silent: true })
    const idle = (window as any).requestIdleCallback as
      | undefined
      | ((cb: () => void, o?: { timeout: number }) => number)
    const handle = idle ? idle(go, { timeout: 1500 }) : window.setTimeout(go, 300)
    return () => {
      cancelled = true
      if (idle) (window as any).cancelIdleCallback?.(handle)
      else window.clearTimeout(handle)
    }
    // runHealth is re-created every render but only touches stable setters + a
    // module fetch; listing it would re-scan the library on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vaults?.served])

  const openLog = () => {
    setPanel('log')
    onLogOpened()
  }
  const openHealth = () => {
    setPanel('health')
    runHealth() // re-run on every open so the report is current (on demand)
  }
  const closePanel = () => setPanel(null)

  // Agent launcher: the primary action always launches the configured default;
  // management is a distinct secondary action on the same icon (right-click /
  // Ctrl+~). A desktop-app agent comes back as mode "app", already open at the
  // vault. A launch that cannot pop a terminal window (or the app) comes back
  // as mode "copy" and the panel shows the `lit agent …` line to run in a
  // terminal on the server (which on a remote/headless box is not the
  // browser's machine).
  const [agentUi, setAgentUi] = useState<AgentUi | null>(null)
  const [agentBusy, setAgentBusy] = useState(false)
  // Machine-global onboarding status — the red-dot source. Fetched once when the
  // TopBar mounts, which only happens while a vault is served (App gates the
  // whole view on `served`), so this mirrors App's served-gated seed: the dot
  // must be known on load, before any click. Unlike the health run above it is
  // neither idle-deferred nor per-vault — the status is machine-global and one
  // cheap read, so it goes out with the rest of the boot fetches.
  const [agentStatus, setAgentStatus] = useState<AgentStatus | null>(null)
  useEffect(() => {
    fetchAgentStatus()
      .then(setAgentStatus)
      .catch(() => {}) // best-effort: no status just means no red dot
  }, [])

  const refreshAgentStatus = () =>
    fetchAgentStatus().then((s) => {
      setAgentStatus(s)
      return s
    })

  // Re-read status and reflect it in both places it shows: the red dot (always,
  // via refreshAgentStatus) and the setup panel if it happens to be open. Drives
  // the auto-recheck-on-focus effect below and the panel's manual "Recheck"
  // button. Best-effort — a failed re-read just leaves the last known state.
  const recheckAgentStatus = () =>
    refreshAgentStatus()
      .then((s) => {
        setAgentUi((ui) =>
          ui?.kind === 'setup' ? { kind: 'setup', status: s } : ui,
        )
        return s
      })
      .catch(() => {})

  // Auto-recheck when the browser regains focus / the tab becomes visible — the
  // "go install the agent CLI (or its skill) in a terminal, come back to the
  // browser" loop, so the red dot clears itself with no manual refresh. Mirrors
  // App.tsx's auto-resync: `focus` fires on app switch, `visibilitychange` on
  // tab switch (both can fire on one return, so an 800ms throttle coalesces).
  // Empty deps is safe: the effect only calls stable setters + a module fetch.
  const lastAgentRecheckRef = useRef(0)
  useEffect(() => {
    const onReturn = () => {
      const now = Date.now()
      if (now - lastAgentRecheckRef.current < 800) return
      lastAgentRecheckRef.current = now
      recheckAgentStatus()
    }
    const onVisible = () => {
      if (document.visibilityState === 'visible') onReturn()
    }
    window.addEventListener('focus', onReturn)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.removeEventListener('focus', onReturn)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  const handleLaunch = (res: AgentLaunchResult) => {
    if (res.permission_warning) {
      notify(res.permission_warning, 'warning')
    }
    if (res.mode === 'spawned') {
      setAgentUi(null)
      if (!res.permission_warning) {
        notify(`Launched ${res.agent} in a terminal window`, 'success')
      }
    } else if (res.mode === 'app') {
      setAgentUi(null)
      if (!res.permission_warning) {
        const display =
          agentStatus?.agents.find((a) => a.name === res.agent)?.display ??
          res.agent
        notify(`Opened ${display}`, 'success')
      }
    } else {
      setAgentUi({ kind: 'copy', agent: res.agent, command: res.command })
    }
  }

  const openAgent = () => {
    if (agentBusy) return
    setAgentBusy(true)
    // Re-read immediately before acting: installation/default state can change
    // outside the browser. A ready default is one click; an invalid/missing one
    // falls into management instead of attempting a command that cannot run.
    refreshAgentStatus()
      .then((status) => {
        if (status.needs_setup) {
          setAgentUi({ kind: 'setup', status })
          return
        }
        return launchAgent().then(handleLaunch)
      })
      .catch((err) =>
        notify(err instanceof Error ? err.message : String(err), 'error'),
      )
      .finally(() => setAgentBusy(false))
  }

  // `openAgent` is rebuilt every render (it closes over agentBusy/agentStatus),
  // so register a STABLE wrapper once and let it read the live closure from a
  // ref — registering openAgent directly would re-fire onRegisterAgentOpen on
  // every render (Cockpit's actionsRef solves the same problem the same way).
  const openAgentRef = useRef(openAgent)
  openAgentRef.current = openAgent
  useEffect(() => {
    onRegisterAgentOpen(() => openAgentRef.current())
    return () => onRegisterAgentOpen(null)
  }, [onRegisterAgentOpen])

  const openAgentManager = () => {
    if (agentBusy) return
    setAgentBusy(true)
    refreshAgentStatus()
      .then((status) => setAgentUi({ kind: 'setup', status }))
      .catch((err) =>
        notify(err instanceof Error ? err.message : String(err), 'error'),
      )
      .finally(() => setAgentBusy(false))
  }

  const openAgentManagerRef = useRef(openAgentManager)
  openAgentManagerRef.current = openAgentManager
  useEffect(() => {
    onRegisterAgentManage(() => openAgentManagerRef.current())
    return () => onRegisterAgentManage(null)
  }, [onRegisterAgentManage])

  const pickAgent = (name: string) => {
    if (agentBusy) return
    setAgentBusy(true)
    launchAgent(name)
      .then(handleLaunch)
      .catch((err) => {
        setAgentUi(null)
        notify(err instanceof Error ? err.message : String(err), 'error')
      })
      .finally(() => setAgentBusy(false))
  }

  // Install/update a skill. Adopting the agent as default is explicit in the
  // button label ("Install skill & use") and never happens as a side effect of
  // merely updating a non-default agent's stale skill.
  const installSkillFor = (name: string, makeDefault = false) => {
    if (agentBusy) return
    setAgentBusy(true)
    let permissionWarning: string | null = null
    installAgentSkill(name)
      .then((installed) => {
        permissionWarning = installed.permission.warning
        return makeDefault ? setDefaultAgent(name) : undefined
      })
      .then(refreshAgentStatus)
      .then((s) => {
        setAgentUi({ kind: 'setup', status: s })
        if (permissionWarning) {
          notify(permissionWarning, 'warning')
        } else {
          notify(
            makeDefault
              ? `${name} is installed and is now your default agent`
              : 'Agent skill installed or updated',
            'success',
          )
        }
      })
      .catch((err) =>
        notify(err instanceof Error ? err.message : String(err), 'error'),
      )
      .finally(() => setAgentBusy(false))
  }

  // Setup panel: the skill is already installed → just adopt the agent as the
  // machine-level default and re-read status (clears the red dot).
  const chooseAgent = (name: string) => {
    if (agentBusy) return
    setAgentBusy(true)
    setDefaultAgent(name)
      .then(refreshAgentStatus)
      .then((s) => {
        setAgentUi({ kind: 'setup', status: s })
        notify(`${name} is now your default agent`, 'success')
      })
      .catch((err) =>
        notify(err instanceof Error ? err.message : String(err), 'error'),
      )
      .finally(() => setAgentBusy(false))
  }

  // Badge counts off the last run (ruling 1): error wins (rose), else warning
  // (amber), else nothing. Empty until the first run.
  const errorCount = healthIssues?.filter((i) => i.severity === 'error').length ?? 0
  const warningCount =
    healthIssues?.filter((i) => i.severity === 'warning').length ?? 0
  const badge =
    errorCount > 0
      ? { count: errorCount, cls: 'bg-rose-500' }
      : warningCount > 0
        ? { count: warningCount, cls: 'bg-amber-500' }
        : null

  // Mirror the manager's open state up so App's shortcut modal-guard sees it.
  useEffect(() => {
    onProjectsOpenChange(showProjects)
  }, [showProjects, onProjectsOpenChange])

  // Same mirror for the vault manager — its nested register form / unregister
  // confirm live inside the panel, so panel-open covers them all.
  useEffect(() => {
    onVaultManagerOpenChange(showVaults)
  }, [showVaults, onVaultManagerOpenChange])

  // Same mirror for the observability panels: while either is open, App's
  // anyModalOpen must be true so global write-shortcuts (⌥R/⌥P) don't fire
  // behind the panel and the panel's own Esc handler owns the key.
  useEffect(() => {
    onObservabilityOpenChange(panel !== null)
  }, [panel, onObservabilityOpenChange])

  // Same mirror for the agent panel.
  useEffect(() => {
    onAgentOpenChange(agentUi !== null)
  }, [agentUi, onAgentOpenChange])

  // Focus mode turns the header into an auto-hiding overlay (macOS fullscreen
  // menu-bar idiom): it detaches to `absolute`, slides up out of view, and
  // slides back down only while the pointer is at the very top edge or a header
  // control holds focus. `revealed` drives that slide; reset whenever focus
  // toggles so re-entering focus always starts hidden.
  const headerRef = useRef<HTMLElement>(null)
  // Whether the pointer is currently over the header. A ref (not state) so the
  // hide check reads it synchronously without forcing a re-render.
  const pointerInside = useRef(false)
  const [revealed, setRevealed] = useState(false)
  useEffect(() => {
    setRevealed(false)
    pointerInside.current = false
  }, [focusMode])

  // Hide only once NEITHER the pointer is over the header NOR a header control
  // holds focus (typing in search, an open vault <select>) — so the bar can't
  // vanish mid-interaction. Re-evaluated on BOTH pointer-leave and focus-leave:
  // if the pointer leaves while a control is still focused the first check bails,
  // and the later blur re-runs it once focus clears. Without that second trigger
  // the bar got stuck open after a search (the reported bug) — mouse-out fired
  // once, bailed on the still-focused search box, and nothing re-checked when
  // focus finally moved away.
  const hideIfIdle = () => {
    if (pointerInside.current) return
    if (headerRef.current?.contains(document.activeElement)) return
    setRevealed(false)
  }

  // The selector shows the SERVED vault, which need not be the registry's
  // active one: `lit gui --vault` serves another registered vault, and `lit gui
  // --library` / $LIT_LIBRARY can serve one that is not registered at all —
  // that one gets an extra first option named after its folder, set apart by
  // colour in the closed select and the open list alike (and the tooltip, for
  // whoever can't tell the colour). Picking it is a no-op, like re-picking any
  // current choice: its value is '' and switchVault ignores ''.
  const served = servedVault(vaults)
  const looseServed = served !== null && !served.registered
  const switchTargets = vaults?.vaults.filter((v) => !v.served).length ?? 0

  return (
    <>
      {focusMode && (
        // Invisible catch-strip pinned to the top edge: hovering it reveals the
        // hidden header. When revealed the header covers this strip.
        <div
          className="absolute inset-x-0 top-0 z-40 h-2"
          onMouseEnter={() => setRevealed(true)}
        />
      )}
      <header
        ref={headerRef}
        onMouseEnter={
          focusMode
            ? () => {
                pointerInside.current = true
                setRevealed(true)
              }
            : undefined
        }
        onMouseLeave={
          focusMode
            ? () => {
                pointerInside.current = false
                hideIfIdle()
              }
            : undefined
        }
        // Focus entering a header control reveals the bar — this is how ⌘K
        // (which focuses the search box) brings the hidden header into view in
        // focus mode. Focus leaving re-checks the hide so the bar drops once
        // both pointer and focus are gone; the relatedTarget guard avoids
        // hiding when focus merely moves between two header controls.
        onFocus={focusMode ? () => setRevealed(true) : undefined}
        onBlur={
          focusMode
            ? (e) => {
                if (
                  !headerRef.current?.contains(e.relatedTarget as Node | null)
                ) {
                  hideIfIdle()
                }
              }
            : undefined
        }
        className={
          'flex items-center gap-2.5 border-b border-stone-200 bg-stone-50/90 px-3 py-2 backdrop-blur-md ' +
          (focusMode
            ? 'absolute inset-x-0 top-0 z-40 transition-transform duration-200 ease-fluid ' +
              (revealed ? 'translate-y-0 shadow-lg shadow-stone-900/10' : '-translate-y-full')
            : 'relative z-30')
        }
      >
      {/* The litman mark doubles as the "What's new" entry point — the same
          card that pops once after an update, reopenable at will. */}
      <button
        type="button"
        className="relative shrink-0 rounded-md transition duration-200 ease-fluid hover:opacity-70"
        title="litman — what's new in this version"
        aria-label="What's new in this version"
        onClick={onShowWhatsNew}
      >
        <LitmanMark className="h-6 w-6 select-none text-stone-800" />
      </button>

      {/* Update chip: a labelled pill, not a bare dot — a 10px dot next to the
          logo reads as part of the artwork; text can't be mistaken for it. */}
      {updateAvailable && (
        <div className="relative shrink-0">
          <button
            type="button"
            onClick={() => setUpdateOpen((v) => !v)}
            title={`litman ${updateAvailable} is available`}
            aria-label={`Update available: litman ${updateAvailable}`}
            className="flex items-center gap-0.5 rounded-full bg-accent-500/10 px-2 py-0.5 text-[11px] font-semibold text-accent-600 transition duration-200 ease-fluid hover:bg-accent-500/20"
          >
            <span aria-hidden="true">↑</span>
            {updateAvailable}
          </button>
          {updateOpen && (
            <>
              <div
                className="fixed inset-0 z-40"
                onClick={() => setUpdateOpen(false)}
              />
              <div className="absolute left-0 top-full z-50 mt-2 w-72 rounded-xl border border-stone-200 bg-stone-50 p-3 shadow-lg shadow-stone-900/10">
                <div className="text-sm font-medium text-stone-800">
                  litman {updateAvailable} is available
                </div>
                {currentVersion && (
                  <div className="mt-0.5 text-xs text-stone-500">
                    You have {currentVersion}.
                  </div>
                )}
                {updateRefused ? (
                  <div className="mt-2 text-xs text-amber-700">{updateRefused}</div>
                ) : (
                  <button
                    type="button"
                    onClick={startSelfUpdate}
                    disabled={updatePhase !== null}
                    className="mt-2 w-full rounded-lg bg-accent-500 px-3 py-1.5 text-sm font-medium text-white transition duration-200 ease-fluid hover:bg-accent-600 disabled:opacity-60"
                  >
                    {updatePhase ? 'Starting…' : 'Update & restart'}
                  </button>
                )}
              </div>
            </>
          )}
          {updatePhase === 'closing' &&
            createPortal(
              <div className="fixed inset-0 z-[80] flex items-center justify-center bg-stone-50/95 backdrop-blur-sm">
                <div className="max-w-sm px-6 text-center">
                  <div className="text-base font-medium text-stone-800">
                    Updating litman…
                  </div>
                  <div className="mt-2 text-sm text-stone-500">
                    This window will close, and litman reopens by itself when
                    the update finishes. If the window stays open, you can
                    close it yourself.
                  </div>
                </div>
              </div>,
              document.body,
            )}
        </div>
      )}

      {!trashMode && (
      <>
      <select
        value={served?.registered ? served.name : ''}
        onChange={(e) => onSwitchVault(e.target.value)}
        disabled={switching || switchTargets === 0}
        title={
          looseServed
            ? 'Opened by path, not in your vault list'
            : switchTargets > 0
            ? 'Switch the active vault (applies globally)'
            : vaults?.vaults.length
              ? 'Only one vault registered'
              : 'No vault registered'
        }
        className={`rounded-md border border-stone-300 bg-white px-2 py-1 text-sm ${
          looseServed ? 'text-amber-700 dark:text-amber-400' : 'text-stone-700'
        } shadow-sm transition-colors hover:bg-stone-50 focus:outline-none focus:ring-1 focus:ring-accent-500 disabled:bg-stone-100 disabled:text-stone-500`}
      >
        {looseServed && (
          <option value="" className="text-amber-700 dark:text-amber-400">
            {served.name}
          </option>
        )}
        {vaults?.vaults.length ? (
          vaults.vaults.map((v) => (
            // A missing vault stays listed and stays selectable: hiding it would
            // be one more thing happening without the user being told. Picking it
            // fails loudly (the server refuses to switch to a path that is gone).
            // "(active)" only where it tells something: normally the served
            // vault IS the active one (a GUI switch moves both), so the marker
            // would just repeat the selection. It shows when `lit gui
            // --library` / `--vault` / $LIT_LIBRARY left the active one behind.
            <option key={v.name} value={v.name} className="text-stone-700">
              {v.name}
              {v.active && !v.served ? ' (active)' : ''}
              {v.exists ? '' : ' — missing'}
            </option>
          ))
        ) : !served ? (
          <option value="">no vault</option>
        ) : null}
      </select>

      {/* Always enabled — NOT gated by the <select>'s <2-vault disable, so a
          single-vault user can still open the manager to register a second. */}
      <button
        type="button"
        onClick={() => setShowVaults(true)}
        title="Register or unregister vaults"
        aria-label="Manage vaults"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-stone-300 bg-white text-stone-500 shadow-sm transition duration-200 ease-fluid hover:bg-stone-50 hover:text-stone-700"
      >
        <IconVault />
      </button>

      <button
        type="button"
        onClick={() => setShowProjects(true)}
        title="Register or remove projects"
        className="flex shrink-0 items-center gap-1.5 rounded-lg border border-stone-300 bg-white px-2.5 py-1 text-sm font-medium text-stone-600 shadow-sm transition duration-200 ease-fluid hover:bg-stone-50 hover:text-stone-900"
      >
        <FolderIcon /> Projects
      </button>

      {showProjects && (
        <ProjectManager
          projects={projects}
          allPapers={allPapers}
          onChanged={onProjectsChanged}
          onClose={() => setShowProjects(false)}
          notify={notify}
        />
      )}

      <SearchBox
        value={search}
        onChange={onSearch}
        candidates={searchCandidates}
        loading={searchLoading}
        onSelect={onSelectResult}
      />
      </>
      )}
      {/* In trash mode the middle controls are gone; this spacer keeps the
          focus/dark/help cluster pinned to the right edge (the SearchBox's
          flex-grow normally does that). */}
      {trashMode && <div className="flex-1" />}

      {/* The vault manager renders in BOTH modes — deliberately outside the
          !trashMode block that hides its toolbar button. The vault-gone
          banner's "Find it" opens it through the registered opener, and a 410
          can arrive in trash mode too (the trash routes sit behind the same
          guard); gated with the button, that click would set state and render
          nothing — a dead recovery button — and the stuck flag would then pop
          the manager open from nowhere on leaving trash mode. A portal, so its
          position in this JSX carries no layout meaning anyway. */}
      {showVaults && (
        <VaultManager
          vaults={vaults}
          onRegisterVault={onRegisterVault}
          onCreateVault={onCreateVault}
          onUnregisterVault={onUnregisterVault}
          onRelocateVault={onRelocateVault}
          onClose={() => setShowVaults(false)}
        />
      )}

      {/* Observability cluster (refresh + log + health), left of the focus/dark/
          help group. All workspace-level, so they sit here in BOTH modes. */}
      <button
        type="button"
        onClick={onRefresh}
        title="Refresh from disk (R) — pull in changes made via the CLI"
        aria-label="Refresh from disk"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconRefresh />
      </button>

      <button
        type="button"
        onClick={openLog}
        title="Activity log — recent actions this session"
        aria-label="Activity log"
        className="relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconLog />
        {logUnread && (
          <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-rose-400 ring-2 ring-stone-50" />
        )}
      </button>

      <button
        type="button"
        onClick={openHealth}
        title="Health check — audit library consistency"
        aria-label="Health check"
        className="relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconShield />
        {badge && (
          <span
            className={`absolute -right-0.5 -top-0.5 grid h-4 min-w-4 place-items-center rounded-full px-1 text-[10px] font-semibold leading-none text-white ring-2 ring-stone-50 ${badge.cls}`}
          >
            {badge.count}
          </span>
        )}
      </button>

      {panel === 'log' && (
        <ActivityLogPanel entries={activityLog} onClose={closePanel} />
      )}
      {panel === 'health' && (
        <HealthPanel
          issues={healthIssues}
          loading={healthLoading}
          error={healthError}
          onRerun={() => runHealth()}
          onClose={closePanel}
        />
      )}

      <button
        type="button"
        onClick={openAgent}
        onContextMenu={(e) => {
          e.preventDefault()
          openAgentManager()
        }}
        disabled={agentBusy}
        title={
          agentStatus?.needs_setup
            ? 'Set up your AI agent'
            : 'Launch default agent — ~ · Right-click or Ctrl+~ to manage'
        }
        aria-label={agentStatus?.needs_setup ? 'Set up agent' : 'Launch agent'}
        className="relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700 disabled:opacity-50"
      >
        <IconAgent />
        {agentStatus?.needs_setup && (
          <span
            aria-label="Agent setup needed"
            className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-rose-500 ring-2 ring-stone-50"
          />
        )}
      </button>

      {agentUi && (
        <AgentPanel
          ui={agentUi}
          busy={agentBusy}
          onPick={pickAgent}
          onInstallSkill={installSkillFor}
          onChooseAgent={chooseAgent}
          onRecheck={recheckAgentStatus}
          onClose={() => setAgentUi(null)}
          notify={notify}
        />
      )}

      {/* Hairline divider between the observability cluster and the view toggles. */}
      <div className="mx-0.5 h-5 w-px shrink-0 bg-stone-200" />

      <button
        type="button"
        onClick={onToggleFocus}
        aria-pressed={focusMode}
        title={focusMode ? 'Exit focus mode' : 'Focus mode — hide side panels'}
        className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg transition duration-200 ease-fluid ${
          focusMode
            ? 'bg-accent-50 text-accent-600'
            : 'text-stone-500 hover:bg-stone-200/70 hover:text-stone-700'
        }`}
      >
        <IconFocus />
      </button>

      <button
        type="button"
        onClick={onToggleDark}
        aria-pressed={dark}
        title={dark ? 'Switch to light mode' : 'Switch to dark mode'}
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        {dark ? <IconSun /> : <IconMoon />}
      </button>

      <button
        type="button"
        onClick={onShowShortcuts}
        title="Keyboard shortcuts (?)"
        aria-label="Keyboard shortcuts"
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-stone-500 transition duration-200 ease-fluid hover:bg-stone-200/70 hover:text-stone-700"
      >
        <IconHelp />
      </button>
      </header>
    </>
  )
}
