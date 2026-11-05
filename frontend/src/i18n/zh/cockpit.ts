import type { Messages } from '../en'

export const cockpit: Messages['cockpit'] = {
  panel: {
    expand: '展开元数据',
    collapse: '收起元数据',
    heading: '元数据',
    trashedHeading: '回收站中的论文',
    loading: '正在加载…',
    selectPaper: '请选择一篇论文。',
    selectTrashed: '请在回收站中选择一篇论文。',
    // 「题录」是 CNKI / NoteExpress 用户熟悉的说法，指 bibliographic fields。
    editTitle: '编辑标题、年份、期刊、作者等题录信息',
    editAria: '编辑元数据',
    copyIdTitle: '复制论文 ID',
    copyId: '复制 ID',
    copyPathTitle: '复制论文文件夹的路径',
    noVaultPath: '无法获取文献库路径',
    copyPath: '复制路径',
    citeTitle: '复制 ACS 格式的引用（期刊缩写、年份、卷、页码）',
    cite: '复制引用',
    copied: {
      id: '✓ 已复制 ID',
      path: '✓ 已复制路径',
      citation: '✓ 已复制引用',
    },
    citeWarnHeading: '引用已复制，请核对：',
    restoring: '正在恢复…',
    restore: '↩ 恢复到文献库',
    orphanRepos: (n: number) =>
      `这篇论文的 ${n} 个代码仓库已从磁盘彻底删除，恢复后需在 CLI 中重新克隆（lit trash restore / lit health-check）。`,
    notReadYet: '尚未标记为已读',
    siDeleted: (file: string) => `已删除 ${file}`,
    siDeletedWithWarnings: (file: string, warnings: string) => `已删除 ${file} · ${warnings}`,
    looksLikeExisting: (value: string) =>
      `“${value}”看起来和已有词条重复，请在列表里找一找。`,
  },

  fields: {
    authors: '作者',
    venueYear: '期刊/会议 · 年份',
    status: '状态',
    type: '类型',
    readDate: '首读日期',
    readRevisit: '首读 / 重读',
    tags: '标签',
    topics: '主题',
    methods: '方法',
    data: '数据',
    projects: '项目',
    relations: '关联论文',
    codeClones: '代码',
    supplementary: '补充材料（SI）',
  },

  readRevisit: {
    unreadAria: '标记为未读',
    unreadTitle: '标记为未读（清除首读日期）',
    firstRead: (date: string) => `首读于 ${date}`,
    markReadTitle: '把今天记为首读日期',
    readOn: (date: string) => `已读 · ${date}`,
    markRead: '标记为已读',
    readFirst: '请先标记为已读',
    revisitTitle: '把今天记为一次重读',
    logRevisit: '记录重读',
    lastRevisited: (date: string) => `上次重读于 ${date}`,
  },

  unreadConfirm: {
    title: '标记为未读？',
    body: (readDate: string | null) =>
      readDate
        ? `将清除首读日期（${readDate}），这篇论文会回到未读状态。`
        : '将清除首读日期，这篇论文会回到未读状态。',
    revisitCleared: (date: string) =>
      `上次重读（${date}）也会一并清除，因为没有首读就不会有重读。此操作无法撤销。`,
    confirm: '标记为未读',
  },

  dropConfirm: {
    title: '搁置这篇论文？',
    body: (title: string) =>
      `“${title}”的状态将改为“搁置”，仍留在列表中，显示为灰色。把状态改回去即可撤销。`,
    confirm: '搁置',
  },

  enumSelect: {
    unset: '（未设置）',
    none: '（无）',
    statusAria: '状态',
    typeAria: '类型',
    gradeAria: (project: string) => `${project} 优先级`,
  },

  codeClones: {
    missingTitle: (name: string) =>
      `codes/${name}/ 不存在，克隆的代码已被删除。可以重新克隆（lit code add），或取消关联（lit code unlink）；lit health-check 也会报告这个问题。`,
    missing: '缺失',
    copyTitle: (name: string) => `复制代码仓库的文件夹路径（codes/${name}）`,
    upstreamAria: (name: string, url: string) => `打开 ${name} 的上游仓库：${url}`,
  },

  tags: {
    nouns: {
      topics: '主题',
      methods: '方法',
      data: '数据',
      projects: '项目',
    },
    pillAria: (label: string, n: number) => `${label}（已选 ${n} 个）`,
    unlink: (project: string) => `取消与 ${project} 的关联`,
    gradeToLink: (project: string) => `选择优先级，把这篇论文关联到 ${project}`,
  },

  createSimilar: {
    title: (value: string) => `新建“${value}”？`,
    similarExist: (noun: string) => `已有相似的${noun}：`,
    use: (value: string) => `改用 ${value}`,
    confirm: '仍然新建',
  },

  vocabPopover: {
    searchAria: (noun: string) => `搜索${noun}`,
    searchPlaceholder: (noun: string) => `搜索${noun}…`,
    // 「词条」= 词表里的一个值（value）。
    noValues: '暂无词条。',
    noMatch: '没有匹配项。',
    create: (value: string) => `新建“${value}”`,
    manage: (noun: string) => `管理${noun}词表…`,
    filtered: (shown: number, total: number) => `${shown} / ${total}`,
    size: (n: number, noun: string) => `${noun}共 ${n} 项`,
  },

  manage: {
    title: (noun: string) => `管理${noun}词表`,
    help: '重命名可修正拼写错误，用到它的论文会一并更新。删除会把这个词条从所有论文上移除。',
    empty: '暂无词条。',
    usedBy: (n: number) => `${n} 篇在用`,
    renameAria: (noun: string, value: string) => `重命名${noun} ${value}`,
    renameTitle: (value: string) => `重命名“${value}”`,
    deleteAria: (noun: string, value: string) => `删除${noun} ${value}`,
    deleteTitle: (value: string) => `删除“${value}”`,
  },

  renameValue: {
    title: (value: string) => `重命名“${value}”？`,
    body: (noun: string, n: number) =>
      n === 0
        ? `将更新${noun}词表。此操作无法撤销。`
        : `将更新${noun}词表，并同步修改 ${n} 篇论文的标签。此操作无法撤销。`,
    collides: (value: string) =>
      `“${value}”已存在，不能重命名为它；请改为删除其中一个。`,
    renaming: '正在重命名…',
    confirm: '重命名',
  },

  deleteValue: {
    title: (value: string) => `删除“${value}”？`,
    body: (value: string, noun: string, n: number) =>
      n === 0
        ? `将从${noun}词表中移除“${value}”。目前没有论文使用它。`
        : `将从${noun}词表中移除“${value}”，并从 ${n} 篇论文上去掉这个标签。此操作无法撤销。`,
  },

  supplementary: {
    opened: (file: string, host: string) => `已在 ${host} 上打开 ${file}`,
    removeAria: (file: string) => `移除 ${file}`,
    removeTitle: '移除',
    add: '添加 SI',
  },

  siRemove: {
    title: '删除这个 SI 文件？',
    checking: '正在检查笔记中是否引用了这个文件…',
    checkFailed: '无法检查笔记中是否引用了这个文件。',
    cited: (where: ReadonlyArray<{ count: number; name: string }>) =>
      `${where.map((c) => `${c.name} 中有 ${c.count} 处引用`).join('，')}。`,
    unchecked: (names: readonly string[]) =>
      `无法检查 ${names.join(' 和 ')} 中是否引用了这个文件。`,
    noUndo: '删除后无法撤销；如需找回，可用“添加 SI”重新添加。',
  },

  relations: {
    removeAria: (rel: string, id: string) => `移除与 ${id} 的关联（${rel}）`,
    // 用「移除」而非「取消关联」：确认框里紧挨着「取消」按钮。
    removeTitle: '移除关联',
    more: (n: number) => `展开其余 ${n} 篇`,
    showLess: '收起',
    confirmTitle: '移除这条关联？',
    confirm: '移除',
  },

  metadataEdit: {
    title: '编辑元数据',
    idTitle:
      'ID 由“年份 + 第一作者 + 标题关键词”组成，wikilink 用它指向这篇论文。要改 ID 请用 `lit rename`，它会同步更新所有引用；这里不能直接编辑。',
    idFixed: '· ID 固定，重命名请用 CLI',
    labels: {
      title: '标题',
      year: '年份',
      journal: '期刊',
      volume: '卷',
      issue: '期',
      pages: '页码',
      publisher: '出版社',
      // venue-type 存的是 CrossRef 类型（journal-article、proceedings-article…）。
      venueType: '文献类型',
      // BibTeX 的 booktitle：所在图书或会议论文集的名称。
      booktitle: '书名/论文集名',
    },
    // `hint` 是 reader.year.hint（「4 位数字」），以数字开头，前面留半角空格。
    yearShape: (hint: string, got: string) => `年份应为 ${hint}，当前为“${got}”。`,
    saving: '正在保存…',
    save: '保存',
  },

  authorRows: {
    label: '作者',
    orderHint: '· 顺序很重要：论文 ID 取自第一作者的姓',
    dragTitle: '拖动调整顺序',
    // 半角逗号是输入格式的一部分。
    placeholder: '姓, 名',
    moveUpTitle: '上移',
    moveUpAria: (n: number) => `将第 ${n} 位作者上移`,
    moveDownTitle: '下移',
    moveDownAria: (n: number) => `将第 ${n} 位作者下移`,
    removeTitle: '移除这位作者',
    removeAria: (n: number) => `移除第 ${n} 位作者`,
    add: '+ 添加作者',
  },

  paperIdField: {
    savedAs: '将保存为 {id}',
    chooseTitle: '点击自行指定 ID',
    label: '论文 ID',
    charset: '仅限 A-Z、0-9、- 和 _',
    // ID 只能是 ASCII：写明「英文」，免得照着标题填进中文。
    keywordPlaceholder: '英文关键词',
    partAria: {
      year: '论文 ID：年份',
      family: '论文 ID：姓氏',
      keyword: '论文 ID：关键词',
    },
    example: '2018_Zhang_Keyword',
    permanent: 'ID 一经保存即固定，之后只能用 `lit rename` 修改。',
  },

  addSi: {
    heading: '补充材料（SI）',
    folder: '不能添加文件夹，请拖入其中的文件。',
    openWebsite: '打开论文网页 ↗',
    downloadThere: '在网页上下载 SI，再把文件拖到这里。',
    noLink: '没有记录论文链接，请把已下载的文件拖到这里。',
    dropHere: '把 SI 文件拖到这里',
    dropHint: '拖入时检查，点“完成”后添加',
    adding: '正在添加…',
    removing: '正在移除…',
    waiting: '正在等待…',
    checking: '正在检查…',
    large: (size: string) => `文件较大（${size}），是否上传？`,
    upload: '上传',
    addAnyway: '仍然添加',
    removeAria: (name: string) => `移除 ${name}`,
    removeTitle: '移除',
  },
}
