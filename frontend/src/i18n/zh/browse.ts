import type { Messages } from '../en'

type TagField = 'topics' | 'methods' | 'data' | 'projects'
type TaxonomyField = 'topics' | 'methods' | 'data'

// 变更记录里的字段名，例如 `X：+主题 protein-design`、`主题词表：+ protein-design`。
const TAG_FIELD: Record<TagField, string> = {
  topics: '主题',
  methods: '方法',
  data: '数据',
  projects: '项目',
}

export const browse: Messages['browse'] = {
  loading: '正在加载…',
  noPaperSelected: '未选中论文',
  openNotes: '📝 笔记',
  openDiscussion: '💬 讨论记录',
  docTab: {
    notes: (id: string) => `${id} · 笔记`,
    discussion: (id: string) => `${id} · 讨论记录`,
  },

  app: {
    selfUpdateFailed: '自动更新失败，当前仍为旧版本',
    siAdded: (n: number) => `已添加 ${n} 个 SI 文件`,
    siAddedWithWarnings: (n: number) => `已添加 ${n} 个 SI 文件 · 文献库索引未更新`,
    refreshed: '已从磁盘刷新',
    crossVaultLink: '这是指向另一个文献库的链接，请先打开那个文献库',
    noSuchPaper: (id: string) => `当前文献库中没有论文“${id}”`,
    removedToTrash: (id: string) => `已将“${id}”移到回收站 · 可在 🗑 回收站中恢复`,
    vaultRegistered: (name: string) => `已将文献库“${name}”添加到列表`,
    vaultCreated: (name: string, path: string) => `已在 ${path} 创建文献库“${name}”`,
    vaultUnregistered: (name: string) => `已将文献库“${name}”从列表移除`,
    vaultRelocated: (name: string, path: string) =>
      `已将文献库“${name}”重新定位到 ${path}`,
    // 「反向关联」= 其他论文指回这篇的关联论文；repo / project 计数是重建的关联，不是重新克隆。
    restored: (r: {
      id: string
      reverseEdges: number
      repos: number
      projects: number
      missingRepos: string[]
    }) => {
      const parts: string[] = []
      if (r.reverseEdges) parts.push(`${r.reverseEdges} 条反向关联`)
      if (r.repos) parts.push(`${r.repos} 个代码仓库关联`)
      if (r.projects) parts.push(`${r.projects} 个项目关联`)
      let msg = `已恢复“${r.id}”`
      if (parts.length) msg += ` · 已重建 ${parts.join('、')}`
      if (r.missingRepos.length)
        msg += ` · ⚠ 代码仓库未重新克隆：${r.missingRepos.join('、')}`
      return msg
    },
    paperAdded: (id: string) => `已添加 ${id}`,
    markedRead: '已标记为已读 · 按 Alt+Shift+R 撤销',
    vaultGone: '文献库已不在 {path}',
    findIt: '重新定位',
    disconnected: '无法连接 litman 服务器，正在重试…',
    linkNotice: '此磁盘不支持文件夹链接，不会生成 {views} 和项目快捷方式。{reassurance}',
    linkNoticeFine: '文献库可正常使用',
    dismiss: '关闭提示',
  },

  resyncLog: {
    paperAdded: (id: string) => `+ ${id} 已添加`,
    paperTrashed: (id: string) => `🗑 ${id} 已移到回收站`,
    paperRemoved: (id: string) => `− ${id} 已移除`,
    statusChanged: (id: string, from: string, to: string) => `${id}：状态 ${from} → ${to}`,
    typeChanged: (id: string, from: string, to: string) => `${id}：类型 ${from} → ${to}`,
    markedRead: (id: string) => `${id}：已标记为已读`,
    readDateCleared: (id: string) => `${id}：已清除首读日期`,
    tagAdded: (id: string, field: TagField, value: string) =>
      `${id}：+${TAG_FIELD[field]} ${value}`,
    tagRemoved: (id: string, field: TagField, value: string) =>
      `${id}：−${TAG_FIELD[field]} ${value}`,
    taxonomyAdded: (field: TaxonomyField, value: string) =>
      `${TAG_FIELD[field]}词表：+ ${value}`,
    taxonomyRemoved: (field: TaxonomyField, value: string) =>
      `${TAG_FIELD[field]}词表：− ${value}`,
    projectAdded: (name: string) => `项目：+ ${name}`,
    projectRemoved: (name: string) => `项目：− ${name}`,
    notesUpdated: (id: string) => `${id}：已更新笔记`,
    discussionAppended: (id: string) => `${id}：已追加讨论记录`,
  },

  browsePanel: {
    listMode: {
      reading: '📖 在读',
      'recent-read': '✅ 已读',
      all: '全部',
    },
    dimension: {
      status: '状态',
      type: '类型',
      topics: '主题',
      methods: '方法',
      data: '数据',
    },
    dimensionNoun: {
      status: '状态',
      type: '类型',
      topics: '主题',
      methods: '方法',
      data: '数据',
    },
    pinTitle: '在列表中置顶',
    unpinTitle: '取消置顶（回到列表中原来的位置）',
    pinAria: (id: string) => `置顶 ${id}`,
    unpinAria: (id: string) => `取消置顶 ${id}`,
    droppedTitle: '搁置：已评估，暂不跟进（保留记录）',
    removeTitle: '从文献库移除这篇论文（移到回收站）',
    removeAria: '从文献库移除论文',
    expandTitle: '展开左侧面板',
    collapseTitle: '收起左侧面板',
    allProjects: '全部项目',
    show: '显示',
    paperCount: (n: number) => `${n} 篇论文`,
    filter: '筛选',
    clearAll: '全部清除',
    removeFilterTitle: '移除此筛选条件',
    noTags: '还没有可用于筛选的标签',
    loadFailed: '无法连接服务器，论文列表加载失败',
    pinned: (n: number) => (n > 0 ? `📌 置顶 · ${n}` : '📌 置顶'),
    unpinAllTitle: '取消所有置顶',
    pinsHidden: (n: number) => `当前视图隐藏了 ${n} 篇置顶论文`,
    noMatch: '没有符合条件的论文',
    emptyTitle: '文献库里还没有论文',
    emptyDrop: '将 PDF 拖入窗口，或交给 Agent 添加',
    trashTitle: '打开回收站（找回已删除的论文）· 最多保留 100 篇',
    trash: '回收站',
  },

  trash: {
    back: '← 返回文献库',
    banner: (vault: string, n: number) =>
      `🗑 回收站${vault ? ` · ${vault}` : ''} · ${n} / 100 · 恢复后回到本文献库（超过 100 篇时自动清除最早的）`,
    filterPlaceholder: '按 ID 或标题筛选…',
    filterAria: '筛选回收站',
    empty: '回收站为空',
    noMatch: '没有匹配的论文',
    restoring: '正在恢复…',
    restore: '↩ 恢复',
    noTabs: '从列表中打开一篇回收站中的论文（只读）',
    tabBadge: '回收站',
    closeTab: '关闭标签页',
    noPreview: '选择一篇回收站中的论文即可预览（只读）',
  },

  projectHealth: {
    missing: {
      badge: '缺失',
      title: '此路径下未找到文件夹，请重新定位项目',
    },
    configOnly: {
      badge: '不完整',
      title: 'TAXONOMY.md 中未列出该项目',
    },
    taxonomyOnly: {
      badge: '不完整',
      title: '未设置项目文件夹',
    },
  },

  projectManager: {
    title: '项目',
    help: '添加、重命名项目，或把项目重新定位到新文件夹。删除项目不会删除磁盘上的项目文件夹。',
    empty: '还没有添加任何项目',
    noFolder: '未设置文件夹',
    paperCount: (n: number) => `${n} 篇论文`,
    renameAria: (name: string) => `重命名项目 ${name}`,
    renameTitle: (name: string) => `重命名项目“${name}”`,
    setPathAria: (name: string) => `更改项目 ${name} 的路径`,
    setPathTitle: (name: string) => `将“${name}”重新定位到新文件夹`,
    deleteAria: (name: string) => `删除项目 ${name}`,
    deleteTitle: (name: string) => `删除项目“${name}”`,
    newProject: '+ 新建项目',
    removed: (name: string) => `已删除项目“${name}”`,
    removedWithLeftovers: (name: string, n: number) =>
      `已删除项目“${name}”，${n} 个链接文件夹未能清理`,
    renamed: (name: string) => `项目已重命名为“${name}”`,
    repointed: (name: string, path: string) => `“${name}”已指向 ${path}`,
    alreadyPointed: (name: string) => `“${name}”已经指向这个位置`,
    linked: (name: string) => `已添加项目“${name}”`,
    deleteConfirmTitle: (name: string) => `删除项目“${name}”？`,
    deleteConfirmBody: (name: string, count: number) =>
      `这会把“${name}”从项目列表移除${
        count > 0 ? `，取消它与 ${count} 篇论文的关联` : ''
      }，并删除它的 reflib 链接和 REFERENCES.md。磁盘上的项目文件夹会保留。此操作无法撤销。`,
    renameDialogTitle: (name: string) => `重命名“${name}”？`,
    renameBody: (count: number) =>
      `所有用到它的地方都会改用新名称${
        count > 0 ? `，并更新 ${count} 篇论文上的项目标签` : ''
      }。磁盘上的文件夹及其路径不变。`,
    nameTaken: (name: string) => `“${name}”已存在，请换一个名称`,
    renaming: '正在重命名…',
    rename: '重命名',
    setPathDialogTitle: (name: string) => `更改“${name}”的路径`,
    setPathBody: '只更新项目列表中记录的路径，不会移动文件夹。请先移动文件夹，再填写新位置。',
    absolutePath: '绝对路径',
    setPathHint: '即项目文件夹本身，必须已存在',
    saving: '正在保存…',
    savePath: '保存路径',
    newDialogTitle: '新建项目',
    newDialogBody: '为已有文件夹指定项目名称，不会新建任何文件。',
    name: '名称',
    newPathHint: '即项目文件夹本身，必须已存在',
    creating: '正在创建…',
    create: '创建',
  },

  welcome: {
    title: '创建文献库',
    subtitle: 'litman 把论文统一存放在本地磁盘的一个文件夹里。',
    location: '存放位置',
    name: '名称',
    creating: '正在创建…',
    create: '创建文献库',
    openExisting: '或者打开已有的文献库',
    open: '打开',
    moved: '缺失',
    locate: '重新定位',
    forget: '从列表移除',
    forgetConfirm: '从列表移除？磁盘上的文件不会被删除。',
    remove: '移除',
  },

  addPaper: {
    title: '添加论文',
    doiPlaceholder: 'DOI（例如 10.1093/nar/gkab813）',
    lookUp: '查询',
    lookingUp: '正在查询…',
    enterManually: '无 DOI 或未查到？手动填写 →',
    backToDoi: '← 改用 DOI 查询',
    titleLabel: '标题',
    yearLabel: '年份',
    journalLabel: '期刊/会议',
    // 这里是出版物类型（期刊论文 / 预印本…），不是论文类型（研究论文 / 综述…）。
    typeLabel: '文献类型',
    manualDoiPlaceholder: '有则填写',
    venueType: {
      'journal-article': '期刊论文',
      'proceedings-article': '会议论文',
      preprint: '预印本',
      patent: '专利',
      book: '图书',
      'book-chapter': '图书章节',
      dissertation: '学位论文',
      report: '报告',
    },
    noAuthors: '（无作者）',
    manyAuthors: (first: string, n: number) => `${first} 等（共 ${n} 位作者）`,
    noTitle: '（无标题）',
    alreadyInLibrary: '文献库中已有这篇论文：{id}',
    originalStays: '原 PDF 文件会留在原处',
    adding: '正在添加…',
    add: '添加到文献库',
  },

  dropZone: {
    onePaperOnly: '一次只能添加一篇论文',
    reading: '正在读取 PDF…',
    lookingForDoi: '正在查找 DOI',
    drop: '松开即可添加 PDF',
    onePaperHint: '一次只能添加一篇',
  },
}
