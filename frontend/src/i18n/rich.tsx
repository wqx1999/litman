import { Fragment, type ReactNode } from 'react'

/** Fill the `{name}` slots of a UI-text template with React nodes, for a
 * sentence that has markup inside it (`Moved {id} to the trash`, the id in
 * <b>). The sentence stays whole in the table, so each language puts its own
 * words in its own order. A slot with no value renders as its literal
 * `{name}`: a visible bug beats a silently dropped word. */
export function rich(
  template: string,
  values: Record<string, ReactNode>,
): ReactNode {
  return template.split(/\{(\w+)\}/).map((part, i) =>
    i % 2 === 0 ? (
      part
    ) : (
      <Fragment key={i}>{part in values ? values[part] : `{${part}}`}</Fragment>
    ),
  )
}
