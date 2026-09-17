/**
 * 主进程入口。
 *
 * 安全基线（不要随意放宽）：
 * - contextIsolation: true，渲染进程与 Electron 内部上下文隔离；
 * - nodeIntegration: false，页面里拿不到 require / process；
 * - sandbox: true，渲染进程运行在系统沙箱中；
 * - 只允许加载本地页面与开发服务器，其余跳转一律交给系统浏览器；
 * - 拒绝一切权限申请（摄像头、定位等本应用都不需要）。
 */

import { BrowserWindow, app, safeStorage, session, shell } from 'electron'
import { join } from 'node:path'

import { registerIpcHandlers } from './ipc'
import { appConfig } from './services/app-config'
import { credentialStore } from './services/credential-store'
import { scheduler } from './services/scheduler'

/** 开发服务器地址，由 electron-vite 注入 */
const DEV_SERVER_URL = process.env['ELECTRON_RENDERER_URL']

/** 是否处于开发模式 */
const isDev = !app.isPackaged

/** 主窗口引用 */
let mainWindow: BrowserWindow | null = null

/**
 * 设置内容安全策略。
 *
 * 开发模式下 Vite 的 HMR 需要内联脚本与 websocket 连接，
 * 因此开发与生产使用两套策略，生产环境收紧到只允许自身资源。
 */
function applyContentSecurityPolicy(): void {
  const policy = isDev
    ? [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self' ws: http://localhost:* http://127.0.0.1:*"
      ].join('; ')
    : [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        // 渲染进程不直接访问后端，网络请求全部由主进程发出，
        // 因此这里不需要放行任何外部地址
        "connect-src 'none'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'"
      ].join('; ')

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [policy]
      }
    })
  })
}

/** 收敛会话权限：本应用不需要任何系统权限 */
function lockDownSession(): void {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false)
  })
  session.defaultSession.setPermissionCheckHandler(() => false)
}

/** 创建主窗口 */
function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    title: '内容创作工作台客户端',
    backgroundColor: '#f5f6f8',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // 渲染进程不需要打开新窗口，一律由主进程接管
      spellcheck: false
    }
  })

  // 首帧渲染完成后再显示，避免白屏闪烁
  mainWindow.on('ready-to-show', () => {
    mainWindow?.show()
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  // 站内跳转一律拦截并交给系统浏览器，防止应用窗口被导航到外部页面
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void openExternalSafely(url)
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (event, url) => {
    const isDevServer = DEV_SERVER_URL && url.startsWith(DEV_SERVER_URL)
    if (!isDevServer) {
      event.preventDefault()
      void openExternalSafely(url)
    }
  })

  // 拒绝渲染进程附加任何 webview
  mainWindow.webContents.on('will-attach-webview', (event) => {
    event.preventDefault()
  })

  if (DEV_SERVER_URL) {
    void mainWindow.loadURL(DEV_SERVER_URL)
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    void mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }

  if (process.env['SMOKE_TEST'] === '1') {
    runSmokeTest(mainWindow)
  }
}

/**
 * 启动自检，由 SMOKE_TEST=1 触发，跑完即退出并返回退出码，供命令行与 CI 使用。
 *
 * 覆盖三条没有任何编译期保障、出问题只会在运行时暴露的路径：
 * 1. sandbox + contextIsolation 下预加载桥是否注入成功（失败表现为整页白屏）；
 * 2. safeStorage 是否可用，以及凭证加解密往返是否一致（安全关键路径）；
 * 3. 配置读写是否会污染默认值。
 *
 * 建议配合 --user-data-dir=<临时目录> 运行，避免动到真实的应用数据。
 */
function runSmokeTest(window: BrowserWindow): void {
  window.webContents.on('preload-error', (_event, preloadPath, error) => {
    console.error(`[smoke] 预加载脚本报错 | ${preloadPath}`, error)
  })

  window.webContents.once('did-finish-load', () => {
    void window.webContents
      .executeJavaScript(
        'typeof window.api === "object" && typeof window.api.tasks.list === "function"'
      )
      .then((bridgeReady: boolean) => {
        const results: Array<{ name: string; passed: boolean; detail: string }> = [
          {
            name: '预加载桥接',
            passed: bridgeReady,
            detail: bridgeReady ? 'window.api 已注入' : 'window.api 未注入'
          },
          checkCredentialStore(),
          checkConfigStore()
        ]

        for (const result of results) {
          console.log(`[smoke] ${result.passed ? '通过' : '失败'} · ${result.name}：${result.detail}`)
        }

        app.exit(results.every((result) => result.passed) ? 0 : 1)
      })
      .catch((error: unknown) => {
        console.error('[smoke] 自检异常', error)
        app.exit(1)
      })
  })
}

/** 自检项：凭证加密往返 */
function checkCredentialStore(): { name: string; passed: boolean; detail: string } {
  const name = '凭证加密'
  // 账号 ID 由数据库自增，从一个极大的值开始几乎不可能与真实账号撞上；
  // 探针跑完会立即删除，即使撞上也只会被覆盖成同一个键，不会造成数据错乱
  const probeAccountId = Number.MAX_SAFE_INTEGER

  try {
    if (!safeStorage.isEncryptionAvailable()) {
      return { name, passed: false, detail: '系统加密能力不可用，凭证保存会被拒绝' }
    }

    const secret = 'smoke-test-secret-value'
    credentialStore.save(probeAccountId, { type: 'cookie', secret })
    const loaded = credentialStore.load(probeAccountId)

    if (loaded?.secret !== secret) {
      return { name, passed: false, detail: '解密结果与原文不一致' }
    }

    // 摘要必须脱敏，不能出现完整凭证
    const summary = credentialStore.summary(probeAccountId)
    if (!summary || summary.maskedSecret.includes(secret)) {
      return { name, passed: false, detail: '凭证摘要未正确脱敏' }
    }

    return { name, passed: true, detail: `加解密一致，脱敏预览为 ${summary.maskedSecret}` }
  } catch (error) {
    return { name, passed: false, detail: error instanceof Error ? error.message : String(error) }
  } finally {
    try {
      credentialStore.remove(probeAccountId)
    } catch {
      // 清理失败不影响自检结论
    }
  }
}

/** 自检项：配置读写 */
function checkConfigStore(): { name: string; passed: boolean; detail: string } {
  const name = '配置读写'
  try {
    const current = appConfig.read()
    const updated = appConfig.update({ pollIntervalSeconds: current.pollIntervalSeconds })
    const passed =
      updated.backendBaseUrl === current.backendBaseUrl &&
      updated.pollIntervalSeconds === current.pollIntervalSeconds
    return {
      name,
      passed,
      detail: passed ? `后端地址 ${updated.backendBaseUrl}` : '读写前后配置不一致'
    }
  } catch (error) {
    return { name, passed: false, detail: error instanceof Error ? error.message : String(error) }
  }
}

/** 只把 http/https 链接交给系统浏览器打开 */
async function openExternalSafely(url: string): Promise<void> {
  try {
    const parsed = new URL(url)
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      await shell.openExternal(url)
    }
  } catch {
    // 非法 URL 直接忽略
  }
}

// 单实例锁：重复启动时聚焦已有窗口，避免两个客户端同时认领任务
const gotTheLock = app.requestSingleInstanceLock()
if (!gotTheLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) {
        mainWindow.restore()
      }
      mainWindow.focus()
    }
  })

  void app.whenReady().then(() => {
    applyContentSecurityPolicy()
    lockDownSession()
    registerIpcHandlers()
    createWindow()

    // 配置里开启了自动执行时，启动即开始消费队列
    if (appConfig.read().autoStartScheduler) {
      scheduler.start()
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        createWindow()
      }
    })
  })
}

app.on('window-all-closed', () => {
  scheduler.stop()
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

// 退出前停掉调度器，避免定时器在关闭过程中继续发起请求
app.on('before-quit', () => {
  scheduler.stop()
})
