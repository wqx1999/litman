import type { Messages } from '../en'

export const reader: Messages['reader'] = {
  cheatSheet: {
    title: '快捷键',
    lang: {
      label: '界面语言',
      auto: '跟随系统',
      autoTitle: (system: 'en' | 'zh') =>
        `跟随系统语言（系统当前是${system === 'zh' ? '中文' : '英文'}）`,
      saveFailed: '语言设置未保存，刷新页面后将恢复原设置',
    },
    or: '或',
    // Where the shortcut works: in a PDF tab, in the note box, with a tool
    // on, in the search box.
    scope: {
      pdf: 'PDF',
      note: '批注',
      tool: '工具',
      search: '搜索框',
    },
    display: {
      title: '显示与面板',
      focusMode: '专注模式（隐藏两侧面板）',
      theme: '切换浅色 / 深色模式',
      leftPanel: '显示 / 隐藏左侧面板',
      cockpit: '显示 / 隐藏右侧面板',
      refresh: '从磁盘刷新',
      cheatSheet: '显示 / 隐藏快捷键列表',
    },
    agent: {
      title: 'Agent',
      // "Plain launch" = the key alone, without Ctrl: 单按.
      note: '数字 1 左边的那个键，按不按 Shift 都可以。默认 Agent 还没配置好时，单按它会打开配置。',
      launch: '启动默认 Agent',
      manage: '管理 Agent / 更换默认 Agent',
    },
    papers: {
      title: '论文列表与搜索',
      note: '在中间的列表里上下移动，按 Enter 打开 PDF。',
      next: '下一篇',
      previous: '上一篇',
      pin: '置顶 / 取消置顶选中的论文',
      open: '打开选中的论文',
      openNotes: '打开笔记',
      openDiscussion: '打开讨论记录',
      focusSearch: '跳到搜索框',
      clearSearch: '清除搜索',
    },
    tabs: {
      title: '标签页',
      note: '拖动标签页可以调整顺序；在标签栏上右键可以一次关闭多个。',
      previous: '上一个标签页',
      next: '下一个标签页',
      jump: '跳到第 1–9 个标签页',
    },
    pdf: {
      title: 'PDF 工具',
      note: '仅在 PDF 标签页中有效。工具之间可以随意切换。撤销只在启用某个工具时可用（“选择”不算）。',
      cursor: '选择（退出当前工具）',
      saveNote: '保存批注并关闭',
      highlight: '高亮',
      textNote: '文字批注',
      draw: '画笔',
      undo: '撤销上一步标注操作',
      redo: '重做',
      back: '回到跳转前的位置',
      forward: '前进到跳转后的位置',
    },
    curation: {
      title: '整理（作用于选中的论文）',
      note: '按住 Alt。作用于选中的论文，未选中论文时无效。',
      // Idempotent: a second press logs nothing more.
      markRead: '标记为已读（重复按只记一次）',
      markUnread: '标记为未读（需确认）',
      promote: '转为精读',
      drop: '搁置（需确认）',
      tagEditor: '打开标签编辑器',
      copyPath: '复制论文路径',
      copyId: '复制论文 ID',
    },
    system: {
      title: '系统快捷键（照常可用）',
      save: '保存当前标签页',
      zoom: 'PDF 缩放',
      focusSearch: '跳到搜索框',
    },
  },

  whatsNew: {
    updated: '已更新',
    // Version first, as in the approved mockup.
    title: (version: string) => `litman ${version} 更新内容`,
    noHighlights: '这个版本没有记录更新要点',
    changelog: '查看完整更新日志 →',
  },

  pdfView: {
    tool: {
      none: {
        label: '选择',
        title: '选取文字、点击链接（双击标注可编辑）',
      },
      highlight: { label: '高亮', title: '高亮文字' },
      freetext: { label: '文字', title: '添加文字批注' },
      ink: { label: '画笔', title: '自由手绘' },
    },
    zoomOut: '缩小',
    zoomIn: '放大',
    zoomPercentLabel: '缩放比例',
    zoomPercentTitle: '输入缩放百分比，按 Enter 确认',
    save: '保存',
    saving: '正在保存…',
    saved: '已保存 ✓',
    saveTitle: '把标注保存进 PDF（⌘/Ctrl+S）',
    noChanges: '没有未保存的改动',
    pageCount: (n: number) => `${n} 页`,
    loadFailed: 'PDF 加载失败',
    notOnThisComputer: '本机没有这篇论文的 PDF',
    loading: '正在加载 PDF…',
    notePlaceholder: '添加批注…',
    deleteAnnotation: '删除标注',
  },

  paramSwatches: {
    color: {
      yellow: '黄色',
      green: '绿色',
      blue: '蓝色',
      pink: '粉色',
      red: '红色',
      black: '黑色',
      orange: '橙色',
    },
    fontSize: (size: number) => `字号 ${size}`,
    thickness: (width: number) => `粗细 ${width}`,
  },

  jumpBackPill: {
    label: (page: number) => `返回第 ${page} 页`,
    title: (page: number) => `返回第 ${page} 页（Alt+←）`,
    text: (page: number) => `返回第 ${page} 页`,
  },

  mdView: {
    header: {
      notes: '📝 笔记',
      discussion: '💬 讨论记录',
    },
    previewingDraft: '草稿未保存（预览中）',
    edit: '编辑',
    preview: '预览',
    save: '保存',
    saving: '正在保存…',
    readOnly: '只读（回收站）',
    unavailable: '无法读取',
    hintStart: '双击开始写',
    hintEdit: '双击编辑',
    saveFailed: (file: string) => `${file} 保存失败，改动已保留，请重试`,
    loadFailed: (file: string) => `无法读取 ${file}`,
    loadFailedFix: '文件未作改动，修复后请重新打开此标签页',
    missingTrashed: (file: string) => `回收站里的这篇论文没有 ${file}`,
    missing: (file: string) => `这篇论文还没有 ${file}，双击开始写`,
  },

  tabArea: {
    noTabs: '从列表中打开一篇论文',
    closeTab: '关闭标签页',
    closeOthers: '关闭其他标签页',
    closeAll: '关闭所有标签页',
    noDocument: '没有打开的文档',
  },

  saveDialog: {
    pdf: {
      title: '要保存标注吗？',
      body: '{label} 有未保存的标注，是否在关闭前保存？',
    },
    md: {
      title: '要保存改动吗？',
      body: '{label} 有未保存的改动，是否在关闭前保存？',
    },
    discard: '不保存',
    save: '保存',
    saving: '正在保存…',
  },

  // The impact lines read as verb phrases under the heading 同时还会：
  removePaper: {
    dialogLabel: '从文献库移除论文',
    title: (id: string) => `把“${id}”移出文献库？`,
    body: '论文将移到回收站，可随时在 🗑 回收站中恢复。',
    previewFailed: '无法预览移除的影响',
    checking: '正在检查会影响哪些内容…',
    nothingLinks: '没有其他内容关联到这篇论文',
    alsoBreaks: '同时还会：',
    references: (papers: string[]) =>
      `从 ${papers.length} 篇论文的关联论文中移除：${papers.join('、')}`,
    reposRemoved: (repos: string[]) =>
      `删除 ${repos.length} 个已克隆的代码仓库（没有其他论文在用）：${repos.join('、')}`,
    reposUnbound: (repos: string[]) =>
      `取消关联 ${repos.length} 个代码仓库（保留：其他论文还在用）：${repos.join('、')}`,
    reposKept: (repos: string[]) =>
      `取消关联 ${repos.length} 个代码仓库（保留：没有来源可以重新克隆）：${repos.join('、')}`,
    reposEdited: (repos: string[]) =>
      `取消关联 ${repos.length} 个代码仓库（保留：有本地改动）：${repos.join('、')}`,
    reposUnchecked: (repos: string[]) =>
      `取消关联 ${repos.length} 个代码仓库（保留：无法检查是否有改动）：${repos.join('、')}`,
    projects: (projects: string[]) =>
      `与 ${projects.length} 个项目取消关联：${projects.join('、')}`,
    // “(deleted)” is the literal tag written into the notes: kept in English.
    notes: (n: number) => `在引用它的 ${n} 份笔记里标上“(deleted)”`,
    removing: '正在移除…',
    remove: '移到回收站',
  },

  externalLinks: {
    noAnswer: 'litman 没有响应，改为在当前窗口打开链接',
  },

  year: {
    hint: '4 位数字',
  },
}
