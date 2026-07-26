import type { SVGProps } from 'react'

// SF-Symbols-style line icons — the same convention as TopBar's inline SVGs
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
  // TopBar's exact pencil path (IconPencil), so the app draws one pen everywhere.
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
