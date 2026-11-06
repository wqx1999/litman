import type { Messages } from '../en'

/** 云同步：文献库选择框旁的云朵按钮及其弹出框、同步留下的提示和操作记录，
 * 以及覆盖了被同步（或 Agent）改过的文件的保存。只说发生了什么，不出现命令、
 * 云端路径或 rclone 原文；弹出框和提示的句末不加句号。 */
const reposNotSynced = (n: number) => `${n} 个代码仓库未能同步`

export const sync: Messages['sync'] = {
  button: {
    title: '云同步',
    notConfigured: '未配置云同步',
    syncNow: '立即同步',
    viewLog: '查看操作记录',
  },
  line: {
    checking: '正在检查…',
    ok: (ago) => `上次同步：${ago}`,
    okUnknown: '已同步',
    okRepos: (ago, n) => `上次同步：${ago}，${reposNotSynced(n)}`,
    running: '正在同步…',
    busy: (since) => (since ? `另一个同步正在进行（${since} 开始）` : '另一个同步正在进行'),
    conflicts: (ago, n) => (n > 0 ? `上次同步：${ago}，${n} 处冲突` : `上次同步：${ago}，存在冲突`),
    offline: (at) => (at ? `暂时无法同步，${at} 自动重试` : '暂时无法同步，稍后自动重试'),
    notInitialised: '本机尚未完成首次同步',
    idle: '启动后尚未同步',
    auth: (name) => (name ? `${name} 登录凭证失效，请重新登录` : '云端登录凭证失效，请重新登录'),
    safetyStop: '单侧改动过多，需确认后继续',
    choice: '需选定以本机或云端为准',
    other: '同步出错，已停止自动同步',
  },
  ago: {
    now: '刚刚',
    minutes: (n) => `${n} 分钟前`,
    hours: (n) => `${n} 小时前`,
    days: (n) => (n === 1 ? '昨天' : `${n} 天前`),
  },
  toast: {
    synced: (parts) => `同步完成：${parts}。详见操作记录`,
    sep: '，',
    conflicts: (n) => `${n} 处冲突`,
    deletedFolders: (n) => `${n} 个文件夹以删除为准`,
    repos: (n) => reposNotSynced(n),
    stopped: (cause) => `自动同步已停止：${cause}`,
    syncNowFailed: '无法开始同步',
  },
  log: {
    conflict: (path, keptHere, backup) =>
      `同步冲突：${path} 在本机和云端均有改动，已保留${keptHere ? '本机' : '云端'}版本，另一版本已备份至 ${backup}`,
    deletedFolder: (folder, here, backup) =>
      `同步：${folder} 已在${here ? '本机' : '其他机器'}删除，另一侧的修改已备份至 ${backup}`,
    repo: (name, kind) =>
      kind === 'failed' || !name
        ? '同步：代码仓库同步出错'
        : kind === 'left-behind'
          ? `同步：代码仓库 ${name} 已从文献库移除，本机文件夹仍保留`
          : `同步：代码仓库 ${name} 未能同步`,
  },
  replaced: (file) => `${file} 在打开后被修改过，已保存你的版本，被覆盖的版本已备份。详见操作记录`,
  replacedLog: (file, backup) => `已覆盖保存 ${file}，被覆盖的版本已备份至 ${backup}`,
}
