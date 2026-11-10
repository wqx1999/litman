import type { Messages } from '../en'
import type { ErrorParams } from '../en/errors'

/** 出错时界面显示的那一句（见 en/errors.ts）。单独显示的一句话句末不加「。」。 */
export const errors: Messages['errors'] = {
  offline: '无法连接 litman 服务器',
  actionFailed: '操作未完成',
  codes: {
    'sync-busy': () => '正在同步，改动未保存，请稍后重试',
    'file-unreadable': (p: ErrorParams) => `无法读取 ${p.path}`,
    'paper-not-found': (p: ErrorParams) => `文献库中已没有论文“${p.id}”`,
    'write-partial': () => '改动未完全保存，下次打开文献库时自动完成',
  },

  cockpit: {
    changeNotSaved: '改动未保存',
    codes: {
      'doi-taken': (p: ErrorParams) => `此 DOI 已属于论文“${p.id}”`,
      'duplicate-author': (p: ErrorParams) => `作者重复：${p.name}`,
      'authors-empty': () => '作者不能为空',
      'project-path-missing': (p: ErrorParams) => `项目“${p.project}”的文件夹在本机不存在`,
      'project-gone': (p: ErrorParams) => `项目“${p.name}”已不在列表中`,
    },
    siDeleteFailed: (file: string) => `未能删除 ${file}`,
    siOpen: {
      'si-missing': (p: ErrorParams) => `未找到 ${p.file}`,
      'cannot-open': (p: ErrorParams) => `无法在本机打开 ${p.file}，已改为下载`,
    },
  },

  cite: {
    'cite-abbrev-unknown': '期刊缩写不在内置表中，已用全称',
    'cite-not-journal': '非期刊论文，ACS 缩写格式可能不适用',
    'cite-preprint': '预印本，按 arXiv 预印本引用',
    'cite-no-venue': '缺少期刊或会议',
    'cite-no-year': '缺少年份',
    'cite-no-volume': '缺少卷号',
    'cite-no-pages': '缺少页码',
  },

  siWindow: {
    codes: {
      'si-request-gone': () => '此窗口已失效，请关闭后重新添加 SI',
    },
    uploadFailed: (file: string) => `${file} 未能保存`,
    notAdded: 'SI 文件未添加',
    verdicts: {
      'si-doi-match': () => '文件名与论文 DOI 一致',
      'si-title-match': () => '首页有论文标题',
      'si-unconfirmed': () => '无法确认属于这篇论文',
      'si-empty': () => '空文件',
      'si-in-library': () => '文献库中已有',
      'si-same-file': (p: ErrorParams) => `与 ${p.name} 是同一文件`,
      'si-is-article': () => '这是论文正文',
      'si-other-paper': (p: ErrorParams) => `像是 ${p.id} 的 SI`,
    },
  },

  paper: {
    notTrashed: '论文未移到回收站',
    restoreCodes: {
      'trash-entry-missing': () => '回收站中已没有这一项',
      'restore-target-exists': (p: ErrorParams) => `文献库中已有“${p.id}”，未恢复`,
    },
    pinNotSaved: '置顶未保存',
  },

  addPaper: {
    'too-large': () => 'PDF 超过 100 MB，无法在界面中添加',
    'not-a-pdf': () => '此文件不是 PDF',
    'crossref-unreachable': () => '无法连接 CrossRef',
    'doi-not-found': () => 'CrossRef 中未找到此 DOI',
    'doi-taken': (p: ErrorParams) => `文献库中已有此 DOI：${p.id}`,
    'id-taken': (p: ErrorParams) => `论文 ID“${p.id}”已被使用`,
    'id-no-author': () => '无法从第一作者姓名生成 ID，请手动填写',
    'id-no-title': () => '无法从标题生成 ID，请手动填写',
    'id-no-year': () => 'CrossRef 记录缺少年份，请手动填写 ID',
    'id-no-first-author': () => 'CrossRef 记录缺少第一作者，请手动填写 ID',
    'upload-expired': () => '上传的文件已失效，请重新拖入 PDF',
    'id-invalid': () => 'ID 只能包含 A-Z、0-9、- 和 _',
  },

  vault: {
    'name-taken': (p: ErrorParams) => `名称“${p.name}”已被使用`,
    'vault-missing': (p: ErrorParams) => `文献库“${p.name}”已不在原位置，未切换`,
    'path-missing': () => '此路径不存在',
    'not-a-vault': () => '此文件夹不是 litman 文献库',
    'already-registered': (p: ErrorParams) => `此文件夹已登记为“${p.other}”`,
    'bad-name': () => '名称只能包含字母、数字和 . _ -',
    'parent-missing': () => '上级文件夹不存在',
    'target-exists': () => '此位置已有同名文件夹',
    'vault-in-use': () => '当前文献库不能从列表移除，请先切换到其他文献库',
  },
  unsavedNotSaved: '未保存的改动保存失败，未切换',

  picker: {
    'folder-missing': () => '文件夹不存在',
    'not-a-folder': () => '不是文件夹',
    'open-denied': () => '没有权限打开此文件夹',
    'mkdir-denied': () => '没有权限在此新建文件夹',
    'mkdir-failed': () => '无法新建文件夹',
    'name-empty': () => '名称不能为空',
    'name-dots': () => '名称不能是“.”或“..”',
    'name-separator': () => '名称不能包含路径分隔符',
    'name-control': () => '名称不能包含换行等控制字符',
    'file-exists': () => '已有同名文件',
  },

  project: {
    'project-exists': (p: ErrorParams) => `项目“${p.name}”已存在`,
    'path-not-absolute': () => '请填写绝对路径',
    'path-missing': () => '此路径不存在',
    'not-a-folder': () => '此路径不是文件夹',
    'project-gone': (p: ErrorParams) => `项目“${p.name}”已不在列表中`,
  },

  agent: {
    statusFailed: '无法读取 Agent 状态',
    codes: {
      'agent-not-installed': (p: ErrorParams) => `${p.agent} 未安装`,
      'agent-open-failed': (p: ErrorParams) => `无法打开 ${p.agent}`,
    },
    permissionsNotWritten: (agent: string) => `权限设置未写入，${agent} 运行 lit 时可能需要逐次确认`,
  },

  updateRefused: '无法在此自动更新',
  healthFailed: '无法运行健康检查',
  linkOpenedHere: '无法在浏览器中打开链接，已在此窗口中打开',
  pdfNotSaved: '标注保存失败，改动已保留，请重试',
}
