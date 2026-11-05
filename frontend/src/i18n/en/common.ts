/** Words shared across areas: generic buttons, and the display labels of the
 * fixed enums. Enum keys are the VALUES the CLI writes (core/schema.py) —
 * never rename a key; only the label on the right is UI text. */
export const common = {
  cancel: 'Cancel',
  done: 'Done',
  delete: 'Delete',
  status: {
    inbox: 'inbox',
    skim: 'skim',
    'deep-read': 'deep-read',
    dropped: 'dropped',
  },
  paperType: {
    research: 'research',
    review: 'review',
    position: 'position',
    benchmark: 'benchmark',
    dataset: 'dataset',
    tutorial: 'tutorial',
    thesis: 'thesis',
    'book-chapter': 'book-chapter',
  },
  relation: {
    extends: 'extends',
    'extended-by': 'extended-by',
    contradicts: 'contradicts',
    'contradicted-by': 'contradicted-by',
    related: 'related',
  },
}
