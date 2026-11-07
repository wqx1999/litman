/** Parameters of a coded failure, as the server sends them (server/errors.py):
 * names, ids, file names, paths inside the library. */
export type ErrorParams = Readonly<Record<string, string>>

/** The lines the page shows when something fails. A failure the server names
 * (its `code`) gets the line for that code; anything else gets the failed
 * action's own line. The server's own text is never shown: it is in the GUI
 * log, where the agent reads it (`lit gui-log`). Picked by `errorLine`
 * (apiErrors.ts). Texts: the prompt-wording audit, server rows. */
export const errors = {
  /** The request got no answer: the server is not running. */
  offline: "Can't reach the litman server.",
  /** The page itself failed, after an answer came back. */
  actionFailed: 'The action did not complete.',
  /** Codes any request can meet. */
  codes: {
    'sync-busy': () => 'A sync is running. The change was not saved; try again shortly.',
    'file-unreadable': (p: ErrorParams) => `Cannot read ${p.path}.`,
    'paper-not-found': (p: ErrorParams) => `Paper “${p.id}” is no longer in the library.`,
    /** Part of a write landed before the disk refused the rest; the next
     * start that opens the library finishes it. */
    'write-partial': () =>
      'The change was only partly saved. litman completes it the next time it opens this library.',
  },

  /** The context panel's writes and the metadata dialog. */
  cockpit: {
    changeNotSaved: 'Change not saved.',
    codes: {
      'doi-taken': (p: ErrorParams) => `This DOI already belongs to “${p.id}”.`,
      'duplicate-author': (p: ErrorParams) => `Duplicate author: ${p.name}`,
      'authors-empty': () => 'Authors cannot be empty.',
      'project-path-missing': (p: ErrorParams) =>
        `The folder of project “${p.project}” does not exist on this computer.`,
      'project-gone': (p: ErrorParams) => `Project “${p.name}” is no longer in the list.`,
    },
    siDeleteFailed: (file: string) => `Could not delete ${file}.`,
    /** Opening a recorded SI file on this machine. */
    siOpen: {
      'si-missing': (p: ErrorParams) => `${p.file} not found.`,
      'cannot-open': (p: ErrorParams) =>
        `Could not open ${p.file} on this computer; downloading it instead.`,
    },
  },

  /** Caveats under "Citation copied — verify:". */
  cite: {
    'cite-abbrev-unknown': 'Journal abbreviation not found; full name used.',
    'cite-not-journal': 'Not a journal article; ACS abbreviations may not apply.',
    'cite-preprint': 'Preprint; cited as arXiv.',
    'cite-no-venue': 'No journal or venue.',
    'cite-no-year': 'No year.',
    'cite-no-volume': 'No volume.',
    'cite-no-pages': 'No pages.',
  },

  /** The SI window. */
  siWindow: {
    codes: {
      'si-request-gone': () => 'This window has expired. Close it and add the SI again.',
    },
    uploadFailed: (file: string) => `${file} could not be saved.`,
    notAdded: 'SI files not added.',
    /** Why a dropped file was or was not taken. */
    verdicts: {
      'si-doi-match': () => "Name matches the paper's DOI",
      'si-title-match': () => "First page has the paper's title",
      'si-unconfirmed': () => 'Cannot confirm it belongs to this paper',
      'si-empty': () => 'Empty file',
      'si-in-library': () => 'Already in the library',
      'si-same-file': (p: ErrorParams) => `Same file as ${p.name}`,
      'si-is-article': () => 'This is the article itself',
      'si-other-paper': (p: ErrorParams) => `Looks like the SI of ${p.id}`,
    },
  },

  /** Removing, restoring and pinning papers. */
  paper: {
    notTrashed: 'The paper was not moved to the trash.',
    restoreCodes: {
      'trash-entry-missing': () => 'This item is no longer in the trash.',
      'restore-target-exists': (p: ErrorParams) =>
        `“${p.id}” already exists in the library; not restored.`,
    },
    pinNotSaved: 'Pin not saved.',
  },

  /** Adding a paper: the drop, the DOI lookup, the Paper ID field, Add. */
  addPaper: {
    'too-large': () => 'PDF is over 100 MB and cannot be added here.',
    'not-a-pdf': () => 'This file is not a PDF.',
    'crossref-unreachable': () => 'Cannot reach CrossRef.',
    'doi-not-found': () => 'DOI not found in CrossRef.',
    'doi-taken': (p: ErrorParams) => `This DOI is already in the library: ${p.id}`,
    'id-taken': (p: ErrorParams) => `Paper ID “${p.id}” is already taken.`,
    'id-no-author': () => "Cannot build an ID from the first author's name; enter one.",
    'id-no-title': () => 'Cannot build an ID from the title; enter one.',
    'id-no-year': () => 'The CrossRef record has no year; enter an ID.',
    'id-no-first-author': () => 'The CrossRef record has no first author; enter an ID.',
    'upload-expired': () => 'This upload has expired. Drop the PDF again.',
    'id-invalid': () => 'The ID may contain only A-Z, 0-9, - and _.',
  },

  /** Libraries: register, create, re-point, switch, unregister. */
  vault: {
    'name-taken': (p: ErrorParams) => `The name “${p.name}” is already taken.`,
    'vault-missing': (p: ErrorParams) =>
      `Library “${p.name}” is no longer at its path; not switched.`,
    'path-missing': () => 'This path does not exist.',
    'not-a-vault': () => 'This folder is not a litman library.',
    'already-registered': (p: ErrorParams) =>
      `This folder is already registered as “${p.other}”.`,
    'bad-name': () => 'Names may contain only letters, digits, “.”, “_” and “-”.',
    'parent-missing': () => 'The parent folder does not exist.',
    'target-exists': () => 'A folder with this name already exists here.',
    'vault-in-use': () =>
      'The vault open in this window cannot be unregistered. Switch to another vault first.',
  },
  /** A switch whose unsaved edits could not be saved first. */
  unsavedNotSaved: 'Unsaved changes could not be saved; not switched.',

  /** The folder picker's line under the address bar. */
  picker: {
    'folder-missing': () => 'Folder not found.',
    'not-a-folder': () => 'Not a folder.',
    'open-denied': () => 'No permission to open this folder.',
    'mkdir-denied': () => 'No permission to create a folder here.',
    'mkdir-failed': () => 'Could not create the folder.',
    'name-empty': () => 'The name cannot be empty.',
    'name-dots': () => 'The name cannot be “.” or “..”.',
    'name-separator': () => 'The name cannot contain path separators.',
    'name-control': () => 'The name cannot contain line breaks or other control characters.',
    'file-exists': () => 'A file with this name already exists.',
  },

  /** The project manager. */
  project: {
    'project-exists': (p: ErrorParams) => `Project “${p.name}” already exists.`,
    'path-not-absolute': () => 'Enter an absolute path.',
    'path-missing': () => 'This path does not exist.',
    'not-a-folder': () => 'This path is not a folder.',
    'project-gone': (p: ErrorParams) => `Project “${p.name}” is no longer in the list.`,
  },

  /** Agents: status, launch, install, default. */
  agent: {
    statusFailed: 'Could not read the agent status.',
    codes: {
      'agent-not-installed': (p: ErrorParams) => `${p.agent} is not installed.`,
      'agent-open-failed': (p: ErrorParams) => `Could not open ${p.agent}.`,
    },
    permissionsNotWritten: (agent: string) =>
      `Permissions not written; ${agent} may ask before each lit command.`,
  },

  updateRefused: 'litman cannot update itself from here.',
  healthFailed: 'Could not run the health check.',
  linkOpenedHere: 'Could not open the link in your browser; opened it here instead.',
  pdfNotSaved: "Couldn't save the annotations — your edits are kept; try again.",
}
