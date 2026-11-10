import type { Messages } from '../en'

export const common: Messages['common'] = {
  cancel: '取消',
  done: '完成',
  delete: '删除',
  status: {
    inbox: '待读',
    skim: '略读',
    'deep-read': '精读',
    dropped: '搁置',
  },
  paperType: {
    research: '研究论文',
    review: '综述',
    position: '观点文章',
    benchmark: '基准测试',
    dataset: '数据集',
    tutorial: '教程',
    thesis: '学位论文',
    'book-chapter': '图书章节',
  },
  relation: {
    extends: '基于',
    'extended-by': '后续工作',
    contradicts: '反驳',
    'contradicted-by': '被反驳',
    related: '相关',
  },
}
