// Dev fallback graph used by `npm run dev` (and any time the injection token is
// unreplaced). Shaped exactly like `build_graph`'s rev-2 output so the renderer
// is exercised against the real contract: a pivot paper (two projects), an
// unassigned paper, a drift value (unregistered topic), a dangling code-clone,
// a broken relation edge, a corrupt paper, and bibliographic `meta` so the
// click-to-open detail card has something to show.

import type { Dimension, GraphData, NodeMeta } from './types'

function dims(partial: Partial<Record<Dimension, string[]>>): Record<Dimension, string[]> {
  return {
    projects: partial.projects ?? [],
    topics: partial.topics ?? [],
    methods: partial.methods ?? [],
    data: partial.data ?? [],
    codes: partial.codes ?? [],
  }
}

function meta(partial: Partial<NodeMeta>): NodeMeta {
  return {
    year: partial.year ?? null,
    authors: partial.authors ?? [],
    n_authors: partial.n_authors ?? partial.authors?.length ?? 0,
    journal: partial.journal ?? '',
    doi: partial.doi ?? '',
    type: partial.type ?? '',
    priority: partial.priority ?? '',
    read_status: partial.read_status ?? '',
  }
}

export const SAMPLE_DATA: GraphData = {
  summary: {
    papers: 6,
    corrupt: 1,
    invalid_edges: 1,
    dimensions: { projects: 2, topics: 4, methods: 1, data: 0, codes: 2 },
  },
  nodes: [
    {
      id: '2023_smith_amp',
      label: 'Predicting antimicrobial peptides',
      type: 'paper',
      status: 'ok',
      degree: 2,
      dims: dims({
        projects: ['pepforge'],
        topics: ['amp', 'graph-nn'],
        methods: ['deep-learning'],
        codes: ['pepforge-repo'],
      }),
      meta: meta({
        year: 2023,
        authors: ['Smith, John', 'Doe, Jane', 'Roe, Richard', 'Lee, Min'],
        n_authors: 4,
        journal: 'Bioinformatics',
        doi: '10.1093/bioinformatics/btad001',
        type: 'research',
        priority: 'A',
        read_status: 'deep-read',
      }),
    },
    {
      id: '2022_lee_helm',
      label: 'HELM-to-SMILES conversion',
      type: 'paper',
      status: 'invalid', // dangling code-clone (ghost-repo)
      degree: 1,
      dims: dims({ projects: ['pepforge'], topics: ['representation'], codes: ['ghost-repo'] }),
      meta: meta({
        year: 2022,
        authors: ['Lee, Min'],
        journal: 'J. Chem. Inf. Model.',
        doi: '10.1021/acs.jcim.2c00001',
        type: 'research',
        read_status: 'skim',
      }),
    },
    {
      id: '2024_doe_bridge',
      label: 'A cross-project bridge paper',
      type: 'paper',
      status: 'ok',
      degree: 3,
      dims: dims({ projects: ['pepforge', 'pepcodec'], topics: ['amp'] }),
      meta: meta({
        year: 2024,
        authors: ['Doe, Jane', 'Smith, John'],
        journal: 'Nat. Mach. Intell.',
        type: 'review',
        priority: 'B',
        read_status: 'inbox',
      }),
    },
    {
      id: '2021_kim_encoder',
      label: 'Peptide encoder/decoder',
      type: 'paper',
      status: 'ok',
      degree: 1,
      dims: dims({ projects: ['pepcodec'], topics: ['representation'], codes: ['codec-repo'] }),
      meta: meta({
        year: 2021,
        authors: ['Kim, Soo', 'Park, Jae', 'Choi, Eun', 'Han, Bo', 'Yoon, Ji'],
        n_authors: 9,
        journal: 'NeurIPS',
        read_status: 'deep-read',
      }),
    },
    {
      id: '2020_orphan_paper',
      label: 'An unassigned paper',
      type: 'paper',
      status: 'ok',
      degree: 1,
      dims: dims({ topics: ['amp'] }),
      meta: meta({ year: 2020, authors: ['Nobody, A.'], read_status: 'inbox' }),
    },
    {
      id: '2025_drift_topic',
      label: 'Paper with an unregistered topic',
      type: 'paper',
      status: 'invalid', // ghost-topic not in TAXONOMY
      degree: 0,
      dims: dims({ projects: ['pepcodec'], topics: ['ghost-topic'] }),
      meta: meta({ year: 2025, authors: ['Ghost, T.'], read_status: 'inbox' }),
    },
    {
      id: '2019_broken_meta',
      label: '2019_broken_meta',
      type: 'corrupt',
      status: 'corrupt',
      degree: 0,
      dims: dims({}),
      meta: meta({}),
    },
  ],
  edges: [
    { source: '2023_smith_amp', target: '2022_lee_helm', type: 'extends', directed: true, weight: 1, status: 'ok' },
    { source: '2023_smith_amp', target: '2024_doe_bridge', type: 'related', directed: false, weight: 1, status: 'ok' },
    { source: '2024_doe_bridge', target: '2021_kim_encoder', type: 'contradicts', directed: true, weight: 1, status: 'ok' },
    { source: '2024_doe_bridge', target: '2020_orphan_paper', type: 'extends', directed: true, weight: 1, status: 'invalid' },
  ],
  dimensions: {
    projects: { values: ['pepcodec', 'pepforge'], invalid: [] },
    topics: { values: ['amp', 'ghost-topic', 'graph-nn', 'representation'], invalid: ['ghost-topic'] },
    methods: { values: ['deep-learning'], invalid: [] },
    data: { values: [], invalid: [] },
    codes: { values: ['codec-repo', 'ghost-repo'], invalid: ['ghost-repo'] },
  },
}
