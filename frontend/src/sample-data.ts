// Dev fallback graph used by `npm run dev` (and any time the injection token is
// unreplaced). Shaped exactly like `build_graph`'s output so the renderer is
// exercised against the real contract: a few projects, papers, code nodes, a
// corrupt node, and an invalid edge.

import type { GraphData } from './types'

export const SAMPLE_DATA: GraphData = {
  summary: { papers: 6, projects: 3, codes: 2, corrupt: 1, invalid_edges: 1 },
  aggregate: {
    nodes: [
      { id: 'pepforge', type: 'project', label: 'pepforge', size: 5, status: 'ok', group: 'pepforge' },
      { id: 'pepcodec', type: 'project', label: 'pepcodec', size: 3, status: 'ok', group: 'pepcodec' },
      { id: 'orphan-proj', type: 'project', label: 'orphan-proj', size: 0, status: 'ok', group: 'orphan-proj' },
    ],
    edges: [
      { source: 'pepforge', target: 'pepcodec', type: 'shared-papers', directed: false, weight: 2, status: 'ok' },
    ],
  },
  drilldown: {
    pepforge: {
      nodes: [
        { id: 'pepforge', type: 'project', label: 'pepforge', size: 5, status: 'ok', group: 'pepforge' },
        { id: '2023_smith_amp', type: 'paper', label: 'Predicting antimicrobial peptides', size: 4, status: 'ok', group: 'pepforge' },
        { id: '2022_lee_helm', type: 'paper', label: 'HELM-to-SMILES conversion', size: 2, status: 'ok', group: 'pepforge' },
        { id: '2024_doe_bridge', type: 'paper', label: 'A cross-project bridge paper', size: 3, status: 'ok', group: 'pepforge' },
        { id: 'pepforge-repo', type: 'code', label: 'pepforge-repo', size: 1, status: 'ok', group: 'pepforge' },
        { id: 'ghost-repo', type: 'code', label: 'ghost-repo', size: 1, status: 'invalid', group: 'pepforge' },
      ],
      edges: [
        { source: 'pepforge', target: '2023_smith_amp', type: 'projects', directed: true, weight: 1, status: 'ok' },
        { source: 'pepforge', target: '2022_lee_helm', type: 'projects', directed: true, weight: 1, status: 'ok' },
        { source: 'pepforge', target: '2024_doe_bridge', type: 'projects', directed: true, weight: 1, status: 'ok' },
        { source: '2023_smith_amp', target: 'pepforge-repo', type: 'code-clones', directed: true, weight: 1, status: 'ok' },
        { source: '2022_lee_helm', target: 'ghost-repo', type: 'code-clones', directed: true, weight: 1, status: 'invalid' },
        { source: '2023_smith_amp', target: '2022_lee_helm', type: 'extends', directed: true, weight: 1, status: 'ok' },
        { source: '2023_smith_amp', target: '2022_lee_helm', type: 'related', directed: false, weight: 1, status: 'ok' },
      ],
    },
    pepcodec: {
      nodes: [
        { id: 'pepcodec', type: 'project', label: 'pepcodec', size: 3, status: 'ok', group: 'pepcodec' },
        { id: '2024_doe_bridge', type: 'paper', label: 'A cross-project bridge paper', size: 3, status: 'ok', group: 'pepforge' },
        { id: '2021_kim_encoder', type: 'paper', label: 'Peptide encoder/decoder', size: 1, status: 'ok', group: 'pepcodec' },
        { id: 'codec-repo', type: 'code', label: 'codec-repo', size: 1, status: 'ok', group: 'pepcodec' },
      ],
      edges: [
        { source: 'pepcodec', target: '2024_doe_bridge', type: 'projects', directed: true, weight: 1, status: 'ok' },
        { source: 'pepcodec', target: '2021_kim_encoder', type: 'projects', directed: true, weight: 1, status: 'ok' },
        { source: '2021_kim_encoder', target: 'codec-repo', type: 'code-clones', directed: true, weight: 1, status: 'ok' },
        { source: '2024_doe_bridge', target: '2021_kim_encoder', type: 'contradicts', directed: true, weight: 1, status: 'ok' },
      ],
    },
    'orphan-proj': {
      nodes: [
        { id: 'orphan-proj', type: 'project', label: 'orphan-proj', size: 0, status: 'ok', group: 'orphan-proj' },
      ],
      edges: [],
    },
    '(unassigned)': {
      nodes: [
        { id: '2020_orphan_paper', type: 'paper', label: 'An unassigned paper', size: 0, status: 'ok', group: '(unassigned)' },
        { id: '2019_broken_meta', type: 'corrupt', label: '2019_broken_meta', size: 0, status: 'corrupt', group: '(unassigned)' },
      ],
      edges: [],
    },
  },
}
