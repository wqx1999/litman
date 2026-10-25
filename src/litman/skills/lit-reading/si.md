# lit-reading — SI on demand, steps 2–4 (B15)

Read this before you run `lit add --si`, or before you look for an answer inside an SI file (SKILL.md B15). Step 1 (`lit show <id>`'s `SI:` lines), the answer format and the red lines are in SKILL.md; its Rules hold here too, and "Rule N" and "Phase 2" refer to it.

2. **Not there → ask litman for it.** First tell the user in one line that you are asking litman for the SI: an SI window appears on their open litman page — Open website, download the SI there, drop the files in, press Done. Then run `lit add --si <id>` (full id or unique substring) as a shell call of its own (Rule 1). It waits up to about 90 s; let it run to its end. If your shell tool times out or moves it to the background, wait for it to finish and read its output — a run your tool cut off counts as exit 11. Rerunning the identical command rejoins the same window; it never opens a second one.

   | Exit | First stdout line(s) | What you do |
   |---|---|---|
   | 0 | `SI ready: N file(s)`, then one line per file: its absolute path, a tab, its original name | Step 4 with those files |
   | 11 | `Still waiting in the GUI — run the same command again to keep waiting` | Run the same command again. After about three runs, answer from the main text and say the SI window is still open — they can tell you once the files are in |
   | 10 | `SI request cancelled in the GUI` | Answer from the main text and say you did not see the SI; don't ask again unless the user does |
   | 12 | `No litman GUI page is open for this library`, often a line saying why, then `Download the SI from the article page: <link>` (or `No link on record — download the SI from the article's page yourself`) and `Then add the files: lit add --si <id> <file>...` | Relay the lines. The user starts `lit gui` (or opens its page) and you rerun — or they download the files and give you the paths (step 3) |
   | 13 | `The paper's text never mentions supplementary material` | Tell the user; rerun with `--force` only if they insist |
   | 1 | `error: …` | Relay it |

   A paper that has SI already returns at once (`… (already in the library)`); when the user says a file is missing, `lit add --si <id> --force` opens the window anyway.
3. **The user hands you files** → `lit add --si <id> FILE...`. Each file gets a line on stderr — `✓` / `?` / `✕`, its original name, size and the reason — and all are added or none:
   - `✕` (`empty file`, `this is the article itself`, `looks like the SI of <other-id>`, `already in the library`, `same file as <name>`) is refused and cannot be added, even with `--force`. Relay it.
   - `?` (`can't confirm it belongs to this paper`), or a file over 100 MiB, needs a yes: the run stops with `… a yes — nothing was added`. Ask the user; add `--force` only on their yes.
   - The files are **moved** into the library — the originals leave Downloads. Say so before you run it.

   Success prints the same `SI ready` lines as exit 0 → step 4.
4. **Find the answer yourself, and say where.** `si-NN` is litman's own numbering (the lowest free number when the file was added), not the publisher's: never infer "Supplementary Data 2" from `si-02`, and never guess from a number which file holds the answer. Search each file:
   - PDF → `lit pdf-text <absolute path>` (works on any PDF) to locate the label or keyword ("Table S3"). It prints no page numbers: pages are separated by form feeds (`\f`), so a hit's page is 1 + the number of `\f` before it. Then read only that page (`lit pdf-text <path> --pages N`, or your file-read tool's page range) — SI PDFs often run to hundreds of pages of spectra. For a figure, or a PDF with no text layer (exit 3), walk the Phase 2 PDF reading ladder and apply its figure rule.
   - xlsx → list the sheet names and header rows first, then read the sheet that matters.
   - csv / txt → read them directly. docx and the rest → your own tools.

   The original names only decide which file to search first. Searched every file and the label is not there (the SI was added only in part) → say which files you searched; on the user's yes, `lit add --si <id> --force` opens the window for the missing file.
