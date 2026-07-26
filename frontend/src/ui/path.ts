// Separator-aware path-string helpers (spec §3.4). The backend returns paths as
// `str(Path)`, so a Windows path arrives with backslashes ("C:\\Users\\you") and
// a POSIX path with slashes ("/home/you"). Rendering or splitting such a path must
// use ITS OWN separator — hard-coding "/" mislabels a Windows path (wrong
// basename, or a breadcrumb trail split on the wrong character). Used by the
// picker breadcrumbs and the create-vault card's location label.

/** The separator a path uses: '\\' for a Windows-looking path (it contains a
 * backslash, or starts with a "C:" drive letter), else '/'. */
export function pathSep(path: string): '\\' | '/' {
  if (path.includes('\\')) return '\\'
  if (/^[A-Za-z]:/.test(path)) return '\\'
  return '/'
}

/** The last segment of a path, using the path's own separator — so a Windows
 * path yields "Desktop" from "C:\\Users\\you\\Desktop" (splitting on '/' used to
 * return the whole string, mislabeling the create-vault card on Windows). */
export function basename(path: string): string {
  const clean = path.trim().replace(/[\\/]+$/, '')
  const sep = pathSep(clean)
  const idx = clean.lastIndexOf(sep)
  return idx >= 0 ? clean.slice(idx + 1) : clean
}

/** One clickable breadcrumb: what to show, and the full path prefix it navigates
 * to. */
export interface Crumb {
  label: string
  path: string
}

/** Split an absolute path into breadcrumb segments, each carrying the path
 * prefix to navigate to. Separator-aware: a Windows path splits on '\\' under a
 * "C:\\" drive root; a POSIX path splits on '/' under a "/" root. Always returns
 * at least one crumb (the root). */
export function breadcrumbs(path: string): Crumb[] {
  return pathSep(path) === '\\' ? windowsCrumbs(path) : posixCrumbs(path)
}

function posixCrumbs(path: string): Crumb[] {
  const parts = path.split('/').filter((s) => s.length > 0)
  const crumbs: Crumb[] = [{ label: '/', path: '/' }]
  let acc = ''
  for (const part of parts) {
    acc += `/${part}`
    crumbs.push({ label: part, path: acc })
  }
  return crumbs
}

function windowsCrumbs(path: string): Crumb[] {
  const parts = path.split(/[\\/]+/).filter((s) => s.length > 0)
  if (parts.length === 0) return [{ label: path, path }]
  // parts[0] is the drive ("C:"); its clickable root is "C:\\".
  const drive = parts[0]
  const root = drive.endsWith(':') ? `${drive}\\` : drive
  const crumbs: Crumb[] = [{ label: root, path: root }]
  let acc = root
  for (const part of parts.slice(1)) {
    acc = acc.endsWith('\\') ? `${acc}${part}` : `${acc}\\${part}`
    crumbs.push({ label: part, path: acc })
  }
  return crumbs
}
