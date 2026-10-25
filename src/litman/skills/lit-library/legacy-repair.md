# lit-library — repair a legacy library (filler metadata / filler ids)

Read this when `lit health-check` reports filler metadata or filler ids in bulk (SKILL.md [K]). SKILL.md's Rules hold here too; "[A]" and "Rule N" refer to SKILL.md.

Trigger: `lit health-check` reports `placeholder_metadata`, `placeholder_id`, `weak_id_keyword` or `dirty_metadata` (the `rename <id>` lines `--fix` prints) — typically dozens or hundreds at once, on a vault filled before those guards existed. The user's phrasing is usually "库里好多 Unknown" / "clean up these bad ids".

**Get the whole list first.** The report caps each category at its first few findings; **`lit health-check --all`** prints every one. Working from the capped view silently repairs 5 papers and reports the category done.

**Fix the fields before the id, always.** The two are separate commands and the order is not cosmetic:

```bash
lit modify <id> --set title="…" --set-author "Family, Given" --set-author "…"
lit rename <old-id> <new-id>
```

Repairing the fields is the half that needs the PDF reopened. Once they are right, **re-run `lit health-check`** — `placeholder_id`'s hint is now a complete, filled-in `lit rename <old> <new>` computed from the corrected metadata, so the second half costs no thinking at all.

🔴 **`--set-author`, one flag per author, the whole list in order.** `--add-tag` appends, which puts a corrected first author last — and the first author's family name is what the id is built from.

**Batch the confirmation, don't batch the decision.** These are permanent handles you are proposing, so they wait for the user's yes (Rule 11) — but asking 300 times is not asking. Present **one table** and take **one yes**:

| old id | proposed | from |
|---|---|---|
| `2018_Unknown_Untitled` | `2018_Zhang_Green-synthesis` | 标题:关于化合物的绿色合成方法 |
| `2019_Unknown_A` | `2019_Li_CRISPR-Cas9` | 标题里的 CRISPR-Cas9 |

Then run the whole batch and report. The user scans, corrects the few they care about, and approves the rest in one message.

**Mark the invented ones.** When a title is in a non-Latin script the keyword is yours to invent, not derive ([A]'s rule) — say which rows are guesses so the user knows where to look. 🔴 Never translate the `title` field to make an id easier.

**What `lit rename` cannot reach.** It rewrites every in-vault link — other papers' `related` / `extends` / `contradicts`, every `[[id]]` in notes, project links, INDEX — and lists, without rewriting, the places notes spell the old id as plain text. It cannot rewrite what left the vault: a `\cite{2024_Unknown_Untitled}` already sitting in the user's manuscript, a link they pasted somewhere. On an old library that is a real risk — **say it once** when proposing the batch, so "一次起对" is a choice and not a surprise.

**Reporting is not fixing.** All three checks are report-only by design: only the reader knows what a paper should be called, so `--fix` never picks a handle. A paper the user chooses to leave alone stays reported — that is correct, not a bug to work around.
