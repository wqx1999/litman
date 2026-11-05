import type { Messages } from '../en'

export const topbar: Messages['topbar'] = {
  places: {
    Home: '主目录',
    Desktop: '桌面',
    Documents: '文档',
    Downloads: '下载',
  },

  topBar: {
    whatsNewTitle: '查看 litman 本版更新',
    whatsNewLabel: '本版更新',
    updateAvailable: (version: string) => `litman ${version} 已发布`,
    updateChipLabel: (version: string) => `有新版本：litman ${version}`,
    youHave: (version: string) => `当前版本：${version}`,
    updateStarting: '正在启动更新…',
    updateAndRestart: '更新并重启',
    updateStartFailed: '无法启动更新，请在终端中运行 `lit self-update`。',
    updatingTitle: '正在更新 litman…',
    updatingBody:
      '此窗口即将关闭，更新完成后 litman 会自动重新打开。如果窗口没有关闭，可以手动关掉。',
    vaultSelectLoose: '按路径打开，不在文献库列表中',
    // "active vault" (what `lit` uses without --library) = 默认文献库; 当前文献库
    // is the one on screen.
    vaultSelectSwitch: '切换默认文献库（全局生效）',
    vaultSelectOnlyOne: '列表中只有一个文献库',
    vaultSelectNone: '列表中还没有文献库',
    vaultOption: (name: string, active: boolean, missing: boolean) => {
      const marks = [active && '默认', missing && '缺失'].filter(Boolean)
      return marks.length ? `${name}（${marks.join('、')}）` : name
    },
    noVault: '无文献库',
    manageVaultsTitle: '添加文献库，或从列表移除',
    manageVaultsLabel: '管理文献库',
    projectsTitle: '添加或删除项目',
    projects: '项目',
    refreshTitle: '从磁盘刷新（R），载入在 CLI 里做的改动',
    refreshLabel: '从磁盘刷新',
    activityLogTitle: '操作记录：本次打开以来的操作',
    activityLogLabel: '操作记录',
    healthTitle: '健康检查：核查文献库的一致性',
    healthLabel: '健康检查',
    agentSetupTitle: '配置 Agent',
    agentLaunchTitle: '启动默认 Agent（~）；右键或按 Ctrl+~ 可管理',
    agentSetupLabel: '配置 Agent',
    agentLaunchLabel: '启动 Agent',
    agentSetupNeeded: '需要配置 Agent',
    focusExit: '退出专注模式',
    focusEnter: '专注模式：隐藏两侧面板',
    lightMode: '切换到浅色模式',
    darkMode: '切换到深色模式',
    shortcutsTitle: '快捷键（?）',
    shortcutsLabel: '快捷键',
    launchedInTerminal: (agent: string) => `已在终端窗口中启动 ${agent}`,
    openedApp: (display: string) => `已打开 ${display}`,
    // The English says "agent is installed"; what gets installed is the skill.
    skillInstalledDefault: (agent: string) =>
      `已为 ${agent} 安装 skill，并设为默认 Agent`,
    skillInstalled: '已安装或更新 Agent 的 skill',
    nowDefault: (agent: string) => `${agent} 已设为默认 Agent`,
  },

  searchBox: {
    placeholder: '搜索 ID、标题、笔记、讨论记录…',
    clearLabel: '清除搜索',
    clearTitle: '清除搜索（Esc）',
    searching: '正在搜索…',
    noMatches: '没有匹配的结果。',
    moreInList: (n: number) => `列表中还有 ${n} 条结果`,
    searchingNotes: '正在搜索笔记…',
    scope: {
      id: 'ID',
      title: '标题',
      author: '作者',
      doi: 'DOI',
      year: '年份',
      notes: '笔记',
      discussion: '讨论记录',
    },
  },

  vaultManager: {
    title: '文献库',
    help: '新建文献库或添加已有的文献库，之后可在工具栏中切换。从列表移除不会删除文件夹。',
    empty: '列表中还没有文献库。',
    active: '默认',
    missing: '缺失',
    missingTitle: '此路径下没有文献库，文件夹可能被移动、重命名或删除了。',
    locate: '重新定位',
    locateLabel: (name: string) => `重新定位文献库 ${name}`,
    locateTitle: (name: string) => `为“${name}”指定新的文件夹`,
    unregisterLabel: (name: string) => `从列表移除文献库 ${name}`,
    unregisterTitle: (name: string) => `从列表移除文献库“${name}”`,
    unregisterServedTitle: '当前文献库不能移除，请先切换到其他文献库',
    newVault: '+ 新建文献库…',
    registerExisting: '+ 添加已有文献库…',
  },

  unregisterVault: {
    title: (name: string) => `从列表移除文献库“${name}”？`,
    body: '此操作只会把“{name}”从文献库列表中移除，{keep}。以后可以重新添加。',
    keepsFolder: '磁盘上的文献库文件夹不会被删除',
    unregistering: '正在移除…',
    unregister: '从列表移除',
  },

  locateVault: {
    title: (name: string) => `重新定位“${name}”`,
    body: '文献库已不在 {path}。请粘贴它现在的路径（指向文献库文件夹本身，其中必须有 lit-config.yaml）。',
    pathLabel: '新的文献库路径',
    locating: '正在重新定位…',
    locate: '重新定位',
  },

  registerVault: {
    title: '添加已有文献库',
    intro:
      '把已有的 litman 文献库添加到列表（不会新建任何内容）。该路径下必须已有 lit-config.yaml。',
    nameLabel: '名称',
    pathLabel: '文献库路径',
    pathHint: '文献库文件夹本身，必须已存在且含有 lit-config.yaml',
    switchAfter: '添加后切换到该文献库',
    registering: '正在添加…',
    register: '添加',
  },

  createVault: {
    title: '新建文献库',
    intro:
      '新建文献库文件夹并添加到列表。所选位置必须已存在，且其中不能已有同名文件夹。',
    locationLabel: '位置',
    locationHint: '文献库会建在这个文件夹下',
    nameLabel: '名称',
    switchAfter: '创建后切换到该文献库',
    creating: '正在创建…',
    create: '创建文献库',
  },

  switchVault: {
    title: (name: string) => `切换到文献库“${name}”？`,
    body: (openTabs: number) =>
      `此操作会{globally}更改默认文献库：所有终端里未指定 {library} / {env} 的 {lit} 命令都会改用它。${
        openTabs > 0 ? `已打开的 ${openTabs} 个标签页会被关闭。` : ''
      }`,
    globally: '全局',
    unsaved: (n: number) => `${n} 个标签页有未保存的改动，切换前会先保存。`,
    switching: '正在切换…',
    saveAndSwitch: '保存并切换',
    switch: '切换',
  },

  healthPanel: {
    title: '健康检查',
    running: '正在检查…',
    rerun: '重新检查',
    // {command} carries its own leading space, so it follows a Chinese
    // character directly.
    intro: '检查文献库的一致性（只读）。修复仍需在 CLI 中运行{command}。',
    runningChecks: '正在运行各项检查…',
    noIssues: '✓ 没有发现问题',
    severity: {
      error: '错误',
      warning: '警告',
      info: '提示',
    },
  },

  activityLog: {
    title: '操作记录',
    // "Cleared on refresh" = reloading the page, not Refresh from disk (R).
    intro: '本次打开以来的操作（最新的在前），刷新页面后清空。',
    empty: '还没有操作记录。',
  },

  agentCopy: {
    title: '在终端中运行',
    body: (agent: string) =>
      `litman 无法从这里打开终端窗口。请在运行 litman 的机器上打开一个终端（如果连接的是远程服务器，就是那台服务器，而不是眼前这台电脑），然后用以下命令启动 ${agent}：`,
    copy: '复制',
    copied: '命令已复制',
    copyFailed: '复制失败，请选中文字后手动复制',
  },

  agentSetup: {
    titleManage: '管理 Agent',
    titleSetup: '配置 Agent',
    help: '工具栏按钮和 ~ 键会打开默认 Agent。装上 skill 后，它运行 lit 时无需每次确认（需该 Agent 支持）。',
    defaultBadge: '默认',
    recheck: '刚装好？重新检测',
    getApp: '获取桌面版 →',
    getCli: '获取 CLI →',
    getAgent: (display: string) => `获取 ${display} →`,
    launch: '启动',
    comingSoon: '即将支持',
    launchWith: '启动方式',
    modeApp: '桌面版',
    modeCli: 'CLI',
    notInstalled: '未安装',
    openOnce: '安装后先打开一次。',
    skill: 'Skill',
    upToDate: '已是最新',
    updateAvailable: '有新版本',
    updating: '正在更新…',
    updateSkill: '更新 skill',
    installing: '正在安装…',
    installSkill: '安装 skill',
    installSkillAndUse: '安装 skill 并设为默认',
    defaultAgent: '默认 Agent',
    opensWith: '点工具栏按钮或按 ~ 键即可打开',
    setDefault: '设为默认',
  },

  directoryPicker: {
    title: '选择文件夹',
    showHidden: '显示隐藏文件夹',
    drivesLabel: '驱动器',
    up: '上一级',
    editPathTitle: '点击编辑路径',
    editPath: '编辑路径',
    newFolder: '新建文件夹',
    newFolderPlaceholder: '新文件夹名称',
    create: '创建',
    loading: '正在加载…',
    // The server sets `denied` only on a PermissionError.
    denied: '没有权限打开这个文件夹。',
    empty: '这里没有子文件夹。',
    undecodableTitle: '这个文件夹的名称不是 UTF-8 文本，重命名后才能在这里打开。',
    vaultBadge: 'litman 文献库',
    notVault: '这个文件夹不是 litman 文献库。',
    select: '选择此文件夹',
  },

  pathField: {
    browse: '浏览…',
  },
}
