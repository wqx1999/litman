import type { SVGProps } from 'react'

// SF-Symbols-style line icons, TopBar's toolbar set included
// (24-grid, no fill, currentColor stroke @ 1.8, round caps). One shared set so
// the picker and the create-vault cards draw from the same pen (no emoji).
// Callers set size + color via className; the default is 18px like TopBar.

type IconProps = SVGProps<SVGSVGElement>

function Line({ className = 'h-[18px] w-[18px]', children, ...rest }: IconProps) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
      {...rest}
    >
      {children}
    </svg>
  )
}

export function HomeIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M4 11.5 12 4l8 7.5" />
      <path d="M6 10v10h12V10" />
      <path d="M10 20v-5h4v5" />
    </Line>
  )
}

export function DesktopIcon(p: IconProps) {
  return (
    <Line {...p}>
      <rect x="3" y="4.5" width="18" height="12" rx="1.6" />
      <path d="M9 20h6M12 16.5V20" />
    </Line>
  )
}

export function DocumentIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M13 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9z" />
      <path d="M13 3v6h6" />
    </Line>
  )
}

export function DownloadIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M12 4v10" />
      <path d="M8 10.5 12 14.5l4-4" />
      <path d="M5 19h14" />
    </Line>
  )
}

export function FolderIcon(p: IconProps) {
  // The exact path TopBar uses, so folders read identically across the app.
  return (
    <Line {...p}>
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    </Line>
  )
}

export function ArrowUpIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M12 19V5" />
      <path d="M6 11l6-6 6 6" />
    </Line>
  )
}

export function PencilIcon(p: IconProps) {
  // Also the rename pencil in the project and vocabulary managers, so the app
  // draws one pen everywhere.
  return (
    <Line {...p}>
      <path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </Line>
  )
}

export function PlusIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M12 5v14M5 12h14" />
    </Line>
  )
}

export function CheckIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M5 12.5l4.5 4.5L19 7" />
    </Line>
  )
}

export function ChevronRightIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M9 6l6 6-6 6" />
    </Line>
  )
}

export function ExternalLinkIcon(p: IconProps) {
  return (
    <Line {...p}>
      <path d="M14 4h6v6" />
      <path d="M20 4l-9 9" />
      <path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10" />
    </Line>
  )
}

export function QuestionCircleIcon(p: IconProps) {
  return (
    <Line {...p}>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.1-2.4 3.7" />
      <path d="M12 17h.01" />
    </Line>
  )
}

/** The line icon for a standard anchor / location kind; a plain folder for
 * anything else. `kind` is an anchor label ('Home'/'Desktop'/'Documents'/
 * 'Downloads') or 'folder'. */
export function anchorIcon(kind: string, className?: string) {
  switch (kind) {
    case 'Home':
      return <HomeIcon className={className} />
    case 'Desktop':
      return <DesktopIcon className={className} />
    case 'Documents':
      return <DocumentIcon className={className} />
    case 'Downloads':
      return <DownloadIcon className={className} />
    default:
      return <FolderIcon className={className} />
  }
}

/** Stacked cylinders (database) — the vault manager. */
export function IconVault() {
  return (
    <Line>
      <ellipse cx="12" cy="5" rx="7" ry="3" />
      <path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" />
      <path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
    </Line>
  )
}

export function IconTrash() {
  return (
    <Line className="h-3.5 w-3.5">
      <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-13" />
    </Line>
  )
}

/** Folder with an arrow — "re-point at a new location" (set-path). */
export function IconMove() {
  return (
    <Line className="h-3.5 w-3.5">
      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v3M3 7v10a2 2 0 0 0 2 2h6M16 16l3 3-3 3M19 19h-6" />
    </Line>
  )
}

/** Viewfinder brackets — "frame the content" (focus mode). */
export function IconFocus() {
  return (
    <Line>
      <path d="M8 3H6a3 3 0 0 0-3 3v2M16 3h2a3 3 0 0 1 3 3v2M8 21H6a3 3 0 0 1-3-3v-2M16 21h2a3 3 0 0 0 3-3v-2" />
    </Line>
  )
}

/** Question mark in a circle — the macOS help-button idiom; opens the
 * keyboard-shortcut cheat sheet (and so reveals the `?` shortcut). */
export function IconHelp() {
  return (
    <Line>
      <circle cx="12" cy="12" r="9" />
      <path d="M9.2 9.3a2.8 2.8 0 0 1 5.4 1c0 1.8-2.7 2.3-2.7 4" />
      <circle cx="12" cy="17.2" r="0.6" fill="currentColor" stroke="none" />
    </Line>
  )
}

export function IconMoon() {
  return (
    <Line>
      <path d="M21 12.8A8.5 8.5 0 1 1 11.2 3a6.5 6.5 0 0 0 9.8 9.8z" />
    </Line>
  )
}

export function IconSun() {
  return (
    <Line>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Line>
  )
}

/** Two circular arrows — refresh / resync the views from disk. */
export function IconRefresh() {
  return (
    <Line>
      <path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
      <path d="M21 3v5h-5" />
      <path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
      <path d="M3 21v-5h5" />
    </Line>
  )
}

/** Clock-with-rewind — the session activity history (D2). */
export function IconLog() {
  return (
    <Line>
      <path d="M3 3v5h5" />
      <path d="M3.05 11A9 9 0 1 0 6 5.3L3 8" />
      <path d="M12 8v4l3 2" />
    </Line>
  )
}

/** Shield-with-check — the library health check (D1). */
export function IconShield() {
  return (
    <Line>
      <path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />
      <path d="M9 12l2 2 4-4" />
    </Line>
  )
}

/** Terminal window with a prompt — launch the configured AI agent. */
export function IconAgent() {
  return (
    <Line>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M7 9.5l3 2.5-3 2.5M12.5 15.5H17" />
    </Line>
  )
}
