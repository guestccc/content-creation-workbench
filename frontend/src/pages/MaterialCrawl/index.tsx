/**
 * 素材抓取页面（图文二创链路的第一步：把各平台素材抓到本地）。
 *
 * 与字幕提取同构：任务由后端调 MediaCrawler 子进程异步执行，页面轮询拿
 * 进度；结果按「跨平台归一化后的笔记表」展示，本地图走 media 接口、
 * 远程图回退直链。
 *
 * 页面自己不写状态机：环境自检走公共 useAsyncData，任务生命周期/历史列表
 * 走公共 useJobRunner / useJobList，表单联动与结果/日志弹窗分别是本文件夹
 * 私有的 useCrawlForm / useCrawlResults，这里只做编排与布局。
 *
 * 抓到的东西往哪去：图片走结果弹窗的「换背景」交给图文二创的下一环
 * （见 swapBackground），视频照旧进镜头分割 / 混剪。
 *
 * 平台 × 模式能力矩阵（支持的模式、输入提示、每页最小条数、Node 依赖）
 * 全部查本文件夹的 platforms.ts，不在 JSX 里写 if 链。
 */

import {
  BgColorsOutlined,
  CloudDownloadOutlined,
  CopyOutlined,
  EyeOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Flex,
  Image,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType, TableProps } from 'antd/es/table'

import HistoryCard from '../../components/HistoryCard'
import JobProgressCard, { JobTitle } from '../../components/JobProgressCard'
import JobRemarkModal from '../../components/JobRemarkModal'
import {
  jobActionsColumn,
  jobCreatedColumn,
  jobIdColumn,
  jobRemarkColumn,
  jobStatusColumn,
} from '../../components/jobColumns'
import {
  useApiMessage,
  useAsyncData,
  useJobList,
  useJobRemark,
  useJobRunner,
  usePurgeFiles,
} from '../../hooks'
import type { UsePurgeFilesResult } from '../../hooks'
import {
  cancelCrawlJob,
  createCrawlJob,
  crawlMediaUrl,
  batchDeleteCrawlJobs,
  deleteCrawlJob,
  fetchCrawlEnvironment,
  fetchCrawlJob,
  fetchCrawlJobs,
  retryCrawlJob,
  updateCrawlJobRemark,
} from '../../api/crawler'
import { fetchCreators } from '../../api/creator'
import type { BackgroundSwapPrefill } from '../../types/background'
import type { CreatorListData } from '../../types/creator'
import {
  CRAWLER_TYPE_META,
  JOB_STATUS_META,
  MEDIA_SUPPORT_LABEL,
  PHASE_META,
  PLATFORM_META,
  PLATFORM_ORDER,
  PLATFORM_SPECS,
  isTerminalStatus,
  noteDisplayTitle,
  parseCountValue,
  phaseSteps,
} from '../../types/crawler'
import type { CrawlPhase } from '../../types/crawler'
import type {
  CrawlEnvironment,
  CrawlJob,
  CrawlJobPayload,
  CrawlLoginType,
  CrawlNote,
  CrawlPlatform,
  CrawlerType,
} from '../../types/crawler'
import { formatDateTime, formatElapsed } from '../../utils/format'
import { useCrawlForm } from './useCrawlForm'
import { useCrawlResults } from './useCrawlResults'
import { useCookieLibrary } from './useCookieLibrary'
import CookieEditModal from './CookieEditModal'
import CookieManagerModal from './CookieManagerModal'

const { Text, Title, Paragraph } = Typography

/** 环境自检里 launcher 命中方式的展示标签 */
const KIND_LABEL: Record<string, string> = {
  'venv-python': 'venv',
  'uv-run': 'uv',
}

export default function MaterialCrawl() {
  const { message, fail, contextHolder } = useApiMessage()
  const navigate = useNavigate()

  const form = useCrawlForm()
  const spec = PLATFORM_SPECS[form.platform]

  // ---------- 环境自检（refresh(true) 绕过缓存，给「重新检测」按钮用） ----------
  const environment = useAsyncData<CrawlEnvironment, boolean>({
    load: (force) => fetchCrawlEnvironment(Boolean(force)),
    failMessage: '环境自检失败',
    silent: true,
    fail,
  })
  const env = environment.data

  // ---------- 创作者库（creator 模式的「从创作者库选择」；库空不渲染选择框） ----------
  const creatorLibrary = useAsyncData<CreatorListData, undefined>({
    load: () => fetchCreators(),
    failMessage: '读取创作者库失败',
    silent: true,
  })
  const libraryCreators = (creatorLibrary.data?.items ?? []).filter(
    (creator) => creator.platform === form.platform,
  )

  // ---------- Cookie 库（cookie 登录的「从 Cookie 库选择 / 存到库」） ----------
  const cookieLib = useCookieLibrary({ message, fail })
  const platformCookies = cookieLib.cookiesForPlatform(form.platform)
  // ---------- 结果 / 日志弹窗 ----------
  const results = useCrawlResults(fail)

  // ---------- 历史 + 当前任务 ----------
  const history = useJobList<CrawlJob>({ fetchList: fetchCrawlJobs })

  // 历史表「备注」列的编辑开关：保存成功后就地刷新列表
  const remark = useJobRemark<CrawlJob>({ message, onSaved: history.reload })

  // 删除时是否连产物一起清（每个删除确认框里都有这个勾选项）
  const purge = usePurgeFiles()

  const runner = useJobRunner<CrawlJob, CrawlJobPayload>({
    create: createCrawlJob,
    cancel: cancelCrawlJob,
    // take() 在这里调用：删除那一刻取值并重置，勾选只对这一次删除有效
    remove: (jobId) => deleteCrawlJob(jobId, purge.take()),
    batchRemove: (ids) => batchDeleteCrawlJobs(ids, purge.take()),
    fetchJob: fetchCrawlJob,
    isTerminal: (job) => isTerminalStatus(job.status),
    fail,
    onChanged: history.reload,
    onFinished: (job) => {
      if (job.note_count > 0) {
        void results.open(job.id)
      }
    },
    onRemoved: (jobId, wasCurrent) => {
      if (wasCurrent) {
        results.closeIfJob(jobId)
      }
    },
  })

  const job = runner.job

  /** 某平台在本机有没有登录态缓存（扫码一次后不用再扫） */
  const loginStateOf = (platform: CrawlPlatform) =>
    env?.login_states.find((state) => state.platform === platform)

  // 该平台签名依赖 Node（抖音/知乎），没 Node 任务会启动失败 → 表单内提示并禁用提交
  const nodeMissing = spec.needsNode && Boolean(env?.ready) && !env?.node_version
  // 知乎创作者模式：原版 MC CLI 缺该分支，未打补丁时禁用提交（环境自检会检测补丁）
  const zhihuCreatorBlocked =
    form.platform === 'zhihu' &&
    form.crawlerType === 'creator' &&
    env !== null &&
    !env.zhihu_creator_cli_supported

  const canSubmit =
    Boolean(env?.ready) && !runner.running && !runner.submitting && !nodeMissing && !zhihuCreatorBlocked

  /** 创建任务：先过表单校验，再交给 runner（runner 负责轮询与失败提示） */
  const start = async () => {
    const built = form.buildPayload()
    if (!built.ok) {
      message.warning(built.error)
      return
    }
    const created = await runner.submit(built.payload)
    if (!created) {
      return
    }
    message.success('任务已创建，等待后台开始抓取')
  }

  const cancel = async (jobId: number) => {
    if (await runner.cancel(jobId)) {
      message.info('已请求取消，正在结束抓取进程')
    }
  }

  /** 删除任务记录（是否连抓取产物一起删由确认框里的勾选决定） */
  const remove = async (jobId: number) => {
    if (await runner.remove(jobId)) {
      message.success('已删除任务记录')
    }
  }

  /** 批量删除历史记录（整批成功或整批失败） */
  const batchRemove = async () => {
    const ids = history.selectedRowKeys
    if (await runner.removeMany(ids)) {
      message.success(`已删除 ${ids.length} 条任务记录`)
      history.clearSelection()
    }
  }

  /** 历史表行多选：只终态任务可选（运行中的任务禁止勾选） */
  const historyRowSelection: TableProps<CrawlJob>['rowSelection'] = {
    selectedRowKeys: history.selectedRowKeys,
    onChange: (keys) => history.setSelectedRowKeys(keys.map(Number)),
    getCheckboxProps: (job) => ({ disabled: !isTerminalStatus(job.status) }),
  }

  /**
   * 重试一条抓取任务。
   *
   * 抓取没有条目级状态（一条任务就是一个 MediaCrawler 子进程），所以只能整任务
   * 重跑；而且必须是**新建**而不是就地重跑：输出目录按任务 id 定死、MC 的 jsonl
   * 又是追加语义，就地重跑会把条数算成两倍。后端就是按这个语义实现的，返回的是
   * 新任务，这里把新 id 告诉用户并刷新列表。
   *
   * cookie 登录的任务也点得动：cookie 只存在数据库行里、任何响应都不带它，
   * 重建这一步在后端做。
   */
  const retryJob = async (target: CrawlJob) => {
    try {
      const created = await retryCrawlJob(target.id)
      history.reload()
      message.success(`已重新发起：新任务 #${created.id}`)
    } catch (error) {
      fail(error, '重试失败')
    }
  }

  /** 从历史记录点开一条任务：加载详情并挂到当前任务区；有结果的顺带打开结果弹窗 */
  const openHistoryJob = async (jobId: number) => {
    const detail = await runner.read(jobId)
    if (!detail) {
      return
    }
    runner.setJob(detail)
    if (isTerminalStatus(detail.status) && detail.note_count > 0) {
      void results.open(jobId)
    }
  }

  /**
   * 结果表点「换背景」：把这条笔记已下载到本地的图片带到换背景页面。
   *
   * 一条笔记的图都落在它自己的一个目录里（后端给的是绝对路径），正好对上那边
   * 「一个原图目录 + 勾选一批文件名」的形态，所以整批带过去并预先勾好 ——
   * 用户到了那边只剩选背景图这一件事。图片是相对路径，取最后一段当文件名
   * （后端固定用 `/` 拼，posix 风格）。
   */
  const swapBackground = (note: CrawlNote) => {
    // 弹窗里有行就一定有 jobId（open 时一起设的）；没有目录说明这条笔记没图
    if (!note.local_image_dir || results.jobId === null) {
      return
    }
    const state: BackgroundSwapPrefill = {
      inputPath: note.local_image_dir,
      files: note.local_images.map((rel) => rel.split('/').pop() ?? rel),
      source: `素材抓取任务 #${results.jobId}`,
    }
    navigate('/background', { state })
  }

  const copyText = (text: string) => {
    void navigator.clipboard.writeText(text).then(
      () => message.success('已复制'),
      () => message.error('复制失败，请手动选择复制'),
    )
  }

  /** 数量输入框下方的说明：三种模式的预估口径不一样 */
  const countHelp =
    form.crawlerType === 'search'
      ? `预计 ≈ 关键词数 × 数量；${spec.label}每页最少 ${spec.searchMinNotes} 条`
      : form.crawlerType === 'detail'
        ? '每条链接抓一条详情'
        : '每位创作者最多抓这么多条'

  return (
    <div className="page">
      {contextHolder}

      <div className="page__header">
        <Title level={3} style={{ marginBottom: 4 }}>
          <CloudDownloadOutlined style={{ marginRight: 8 }} />
          素材抓取
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 0 }}>
          从小红书 / 抖音 / 快手 / B站 / 微博 / 贴吧 / 知乎抓图文与视频素材到本地，
          支持关键词搜索、指定笔记、创作者主页三种方式。
        </Paragraph>
      </div>

      {/* ---------- 环境自检 ---------- */}
      {env && !env.ready && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message="未检测到可用的 MediaCrawler"
          description={
            <Space direction="vertical" size={8} style={{ width: '100%' }}>
              {env.detail && <Text>{env.detail}</Text>}
              {env.install_hints.map((hint, index) => (
                <div key={index}>
                  <Space size={8}>
                    <Text strong>{hint.title}</Text>
                    {hint.command && (
                      <>
                        <Text code style={{ fontSize: 12 }}>
                          {hint.command}
                        </Text>
                        <Button
                          size="small"
                          type="text"
                          icon={<CopyOutlined />}
                          onClick={() => copyText(hint.command)}
                        />
                      </>
                    )}
                  </Space>
                  {hint.note && (
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {hint.note}
                      </Text>
                    </div>
                  )}
                  {hint.url && (
                    <div>
                      <a href={hint.url} target="_blank" rel="noreferrer" style={{ fontSize: 12 }}>
                        {hint.url}
                      </a>
                    </div>
                  )}
                </div>
              ))}
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={environment.loading}
                onClick={() => void environment.reload(true)}
              >
                重新检测
              </Button>
            </Space>
          }
        />
      )}

      {env?.ready && (
        <Alert
          type="success"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space size={8} wrap>
              <span>环境正常</span>
              {env.kind && <Tag color="green">MediaCrawler（{KIND_LABEL[env.kind] ?? env.kind}）</Tag>}
              {env.python_version && <Tag>Python {env.python_version}</Tag>}
              {env.node_version ? (
                <Tag>Node {env.node_version}</Tag>
              ) : (
                <Tag color="orange">Node 未安装（抖音 / 知乎需要）</Tag>
              )}
            </Space>
          }
          description={
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space size={8} wrap>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  目录：{env.mc_root}
                </Text>
                <Tag color={env.media_enabled ? 'green' : 'orange'}>
                  {env.media_enabled ? '媒体下载已开启' : '媒体下载未开启'}
                </Tag>
                {env.login_states.length > 0 && (
                  <Space size={4} wrap>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      已缓存登录态：
                    </Text>
                    {env.login_states.map((state) => (
                      <Tag key={state.platform} color="green">
                        {state.platform_label}
                      </Tag>
                    ))}
                  </Space>
                )}
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>
                MediaCrawler 为非商用学习协议，本功能仅供个人学习研究自用，请勿商用或分发抓取内容。
              </Text>
            </Space>
          }
        />
      )}

      {/* 环境可用但需要注意的情况（如 zhihu 补丁未打、媒体目录异常） */}
      {env && env.warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            <Space direction="vertical" size={4}>
              {env.warnings.map((text) => (
                <span key={text}>{text}</span>
              ))}
            </Space>
          }
        />
      )}

      {/* ---------- 抓取参数 ---------- */}
      <Card
        style={{ marginBottom: 16 }}
        title={
          <Space size={8}>
            <CloudDownloadOutlined style={{ color: 'var(--color-primary)' }} />
            抓取参数
          </Space>
        }
      >
        <Space direction="vertical" size={16} style={{ width: '100%' }}>
          <Row gutter={16}>
            <Col xs={24} md={12}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                平台
              </Text>
              <Select<CrawlPlatform>
                value={form.platform}
                onChange={form.setPlatform}
                style={{ width: '100%', marginTop: 4 }}
                options={PLATFORM_ORDER.map((key) => {
                  const target = PLATFORM_SPECS[key]
                  const cached = loginStateOf(key)
                  return {
                    value: key,
                    label: (
                      <Flex justify="space-between" align="center" gap={8}>
                        <span>{target.label}</span>
                        {cached && (
                          <Tag color="green" style={{ marginInlineEnd: 0 }}>
                            已缓存登录态
                          </Tag>
                        )}
                      </Flex>
                    ),
                  }
                })}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                可下载媒体：{MEDIA_SUPPORT_LABEL[spec.media]}
                {env && !env.media_enabled ? '（媒体下载当前未开启）' : ''}
              </Text>
            </Col>
            <Col xs={24} md={12}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                抓取模式
              </Text>
              <div style={{ marginTop: 4 }}>
                <Segmented
                  value={form.crawlerType}
                  onChange={(value) => form.setCrawlerType(value as CrawlerType)}
                  options={spec.modes.map((mode) => ({
                    value: mode,
                    label: CRAWLER_TYPE_META[mode].label,
                  }))}
                />
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {form.crawlerType === 'search'
                  ? '按关键词搜索平台内容'
                  : form.crawlerType === 'detail'
                    ? '抓指定笔记 / 作品的详情'
                    : '抓指定创作者发布的内容'}
              </Text>
            </Col>
          </Row>

          {/* 模式专属输入：label / placeholder / 说明随平台×模式切换 */}
          {form.crawlerType === 'search' && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                关键词（每行一个，支持逗号分隔）
              </Text>
              <Input.TextArea
                value={form.keywordsText}
                onChange={(event) => form.setKeywordsText(event.target.value)}
                rows={3}
                placeholder={'保温杯\n焖烧杯'}
                style={{ marginTop: 4 }}
              />
            </div>
          )}
          {form.crawlerType === 'detail' && (
            <div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                笔记 / 作品链接或 ID（每行一条）
              </Text>
              <Input.TextArea
                value={form.idsText}
                onChange={(event) => form.setIdsText(event.target.value)}
                rows={3}
                placeholder={spec.detailPlaceholder}
                style={{ marginTop: 4 }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {spec.detailHelp}
              </Text>
            </div>
          )}
          {form.crawlerType === 'creator' && (
            <div>
              {libraryCreators.length > 0 && (
                <div style={{ marginBottom: 8 }}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    从创作者库选择（选中即按行并入下方输入框）
                  </Text>
                  <Select
                    mode="multiple"
                    value={form.librarySelection}
                    onChange={form.setLibrarySelection}
                    style={{ width: '100%', marginTop: 4 }}
                    placeholder="可多选；也可以跳过这里直接手填"
                    maxTagCount="responsive"
                    allowClear
                    options={libraryCreators.map((creator) => ({
                      value: creator.homepage,
                      label:
                        creator.tags.length > 0
                          ? `${creator.name}（${creator.tags.join('、')}）`
                          : creator.name,
                    }))}
                  />
                </div>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                创作者主页链接或 ID（每行一条）
              </Text>
              <Input.TextArea
                value={form.creatorsText}
                onChange={(event) => form.setCreatorsText(event.target.value)}
                rows={3}
                placeholder={spec.creatorPlaceholder}
                style={{ marginTop: 4 }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {spec.creatorHelp}
              </Text>
            </div>
          )}

          {/* 登录方式：扫码（推荐，登录态缓存）或 Cookie */}
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              登录方式
            </Text>
            <div style={{ marginTop: 4 }}>
              <Radio.Group
                optionType="button"
                buttonStyle="solid"
                value={form.loginType}
                onChange={(event) => form.setLoginType(event.target.value as CrawlLoginType)}
              >
                <Radio.Button value="qrcode">扫码登录</Radio.Button>
                <Radio.Button value="cookie">Cookie 登录</Radio.Button>
              </Radio.Group>
            </div>
            {form.loginType === 'qrcode' ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {loginStateOf(form.platform)
                  ? `${PLATFORM_META[form.platform].label}已有登录缓存，一般不会再弹窗`
                  : '首次会弹出 Chrome 窗口扫码，登录态会缓存，之后同类任务不再弹窗'}
              </Text>
            ) : (
              <div style={{ marginTop: 4 }}>
                <Flex gap={8} align="center">
                  <Select
                    value={cookieLib.selectedId}
                    onChange={(id) => {
                      if (id === undefined || id === null) {
                        cookieLib.setSelectedId(null)
                        return
                      }
                      void cookieLib.select(id).then((value) => {
                        if (value !== null) {
                          form.setCookies(value)
                        }
                      })
                    }}
                    style={{ flex: 1 }}
                    placeholder={
                      platformCookies.length > 0
                        ? '从 Cookie 库选择（选中即回填下方）'
                        : 'Cookie 库还是空的，先粘贴再「存到库」'
                    }
                    allowClear
                    disabled={platformCookies.length === 0}
                    options={platformCookies.map((item) => ({
                      value: item.id,
                      label: item.remark ? `${item.name}（${item.remark}）` : item.name,
                    }))}
                  />
                  <Button onClick={cookieLib.openSave} disabled={!form.cookies.trim()}>
                    存到库
                  </Button>
                  {cookieLib.items.length > 0 && (
                    <Button onClick={() => cookieLib.setManagerOpen(true)}>管理库</Button>
                  )}
                </Flex>
                <Input.TextArea
                  value={form.cookies}
                  onChange={(event) => form.setCookies(event.target.value)}
                  rows={3}
                  placeholder="粘贴登录后浏览器里的 Cookie 串"
                  style={{ marginTop: 8 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  存到库后下次直接选，不用再翻浏览器开发者工具；同名会覆盖
                </Text>
              </div>
            )}
          </div>

          {/* 数量与评论 */}
          <Row gutter={16}>
            <Col xs={24} md={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                抓取数量上限
              </Text>
              <InputNumber
                min={1}
                max={200}
                value={form.maxNotes}
                onChange={(value) => form.setMaxNotes(Math.max(1, value ?? 1))}
                addonAfter="条"
                style={{ width: '100%', marginTop: 4 }}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {countHelp}
              </Text>
            </Col>
            {form.crawlerType === 'search' && (
              <Col xs={24} md={8}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  起始页码
                </Text>
                <InputNumber
                  min={1}
                  max={100}
                  value={form.startPage}
                  onChange={(value) => form.setStartPage(Math.max(1, value ?? 1))}
                  style={{ width: '100%', marginTop: 4 }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  从第几页开始抓，翻旧内容用
                </Text>
              </Col>
            )}
            <Col xs={24} md={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                评论
              </Text>
              <div style={{ marginTop: 4 }}>
                <Space wrap>
                  <Space size={4}>
                    <Switch checked={form.getComments} onChange={form.setGetComments} />
                    <Text style={{ fontSize: 12 }}>抓评论</Text>
                  </Space>
                  <Space size={4}>
                    <Switch
                      checked={form.getSubComments}
                      disabled={!form.getComments}
                      onChange={form.setGetSubComments}
                    />
                    <Text style={{ fontSize: 12 }}>二级评论</Text>
                  </Space>
                  <InputNumber
                    min={0}
                    max={200}
                    disabled={!form.getComments}
                    value={form.maxComments}
                    onChange={(value) => form.setMaxComments(Math.max(0, value ?? 0))}
                    addonAfter="条/笔记"
                    style={{ width: 140 }}
                  />
                </Space>
              </div>
            </Col>
          </Row>

          {/* 平台级前置问题：Node 缺失 / 知乎创作者补丁 */}
          {nodeMissing && (
            <Alert
              type="warning"
              showIcon
              message="该平台需要 Node.js"
              description="抖音 / 知乎的签名依赖 Node.js（pyexecjs），当前未检测到，任务会启动失败。请安装 Node.js 16 及以上版本后重新检测。"
            />
          )}
          {zhihuCreatorBlocked && (
            <Alert
              type="warning"
              showIcon
              message="知乎创作者模式需要补丁"
              description="MediaCrawler 原版 CLI 不支持知乎的 --creator_id（缺对应分支）。可先用关键词搜索或指定笔记模式，或按 MC 仓库说明打补丁后重新检测。"
            />
          )}

          {/* 高级选项：默认值就够用，收起来不打扰 */}
          <Collapse
            ghost
            items={[
              {
                key: 'advanced',
                label: <Text type="secondary" style={{ fontSize: 12 }}>高级选项</Text>,
                children: (
                  <Flex vertical gap={18}>
                    <Flex justify="space-between" align="flex-start" gap={16}>
                      <div>
                        <Text>无头模式</Text>
                        <div>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {form.loginType === 'qrcode'
                              ? '扫码登录时二维码由系统看图软件弹出，无头也能扫；若遇到滑块等验证，请关掉无头后重试'
                              : '开启后抓取全程不显示浏览器窗口，适合挂机批量抓'}
                          </Text>
                        </div>
                      </div>
                      <Switch checked={form.headless} onChange={form.setHeadless} />
                    </Flex>
                    <Flex justify="space-between" align="flex-start" gap={16}>
                      <div>
                        <Text>并发抓取数</Text>
                        <div>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            同时抓几路；并发越高越容易触发平台风控，保持 1 最稳
                          </Text>
                        </div>
                      </div>
                      <InputNumber
                        min={1}
                        max={3}
                        value={form.maxConcurrency}
                        onChange={(value) => form.setMaxConcurrency(Math.min(3, Math.max(1, value ?? 1)))}
                        addonAfter="路"
                        style={{ width: 110 }}
                      />
                    </Flex>
                  </Flex>
                ),
              },
            ]}
          />

          {/* 动作区 */}
          <Flex align="center" justify="space-between" wrap gap={12}>
            <Space>
              <Button
                type="primary"
                icon={<CloudDownloadOutlined />}
                loading={runner.submitting}
                disabled={!canSubmit}
                onClick={() => void start()}
              >
                开始抓取
              </Button>
              {runner.running && job && (
                <Popconfirm
                  title="取消当前任务？"
                  description="已抓到的笔记与媒体会保留，MC 进程会被整组结束。"
                  okText="取消任务"
                  cancelText="继续抓"
                  onConfirm={() => void cancel(job.id)}
                >
                  <Button danger>停止</Button>
                </Popconfirm>
              )}
            </Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {!env?.ready
                ? '请先按上方指引安装 MediaCrawler'
                : '同时只跑一个任务，后来的排队等待；结果落到素材目录的 crawl 分段'}
            </Text>
          </Flex>
        </Space>
      </Card>

      {/* ---------- 当前任务 ---------- */}
      {job && (
        <JobProgressCard
          title={<JobTitle jobId={job.id} meta={JOB_STATUS_META[job.status]} />}
          status={job.status}
          percent={job.progress_percent}
          running={runner.running}
          stats={[
            { label: '平台', value: PLATFORM_META[job.platform].label },
            { label: '模式', value: CRAWLER_TYPE_META[job.crawler_type].label },
            { label: '已抓 / 预计', value: `${job.crawled_count} / ${job.expected_count} 条` },
            {
              label: '笔记',
              value: `${job.note_count > 0 ? job.note_count : job.crawled_count} 条`,
            },
            {
              label: '耗时',
              value: job.elapsed_seconds > 0 ? formatElapsed(job.elapsed_seconds) : '—',
            },
          ]}
          errorMessage={job.error_message}
          outputDir={job.output_dir}
        >
          {(job.status === 'pending' || job.status === 'running') && (
            <PhaseSteps job={job} />
          )}
          <Space style={{ marginTop: 12 }}>
            {(job.note_count > 0 || job.crawled_count > 0) && (
              <Button icon={<EyeOutlined />} onClick={() => void results.open(job.id)}>
                查看结果
              </Button>
            )}
            <Button onClick={() => void results.loadLog(job.id)}>日志尾部</Button>
          </Space>
        </JobProgressCard>
      )}

      {/* ---------- 历史任务 ---------- */}
      <HistoryCard
        columns={historyColumns({
          onView: (id) => void openHistoryJob(id),
          onCancel: (id) => void cancel(id),
          onDelete: (id) => void remove(id),
          onRetry: (target) => void retryJob(target),
          onEditRemark: remark.open,
          purge,
        })}
        dataSource={history.items}
        loading={history.loading}
        onRefresh={history.reload}
        rowSelection={historyRowSelection}
        pagination={{
          total: history.total,
          pageSize: 10,
          showSizeChanger: false,
          current: history.page,
          onChange: history.setPage,
        }}
        // 这页列最多（平台 / 模式 / 内容都占宽）：窄屏改成横向滚动，
        // 别让「内容」这个弹性列被备注列压成一条缝
        scroll={{ x: 1080 }}
        extra={
          <Popconfirm
            title={`删除这 ${history.selectedRowKeys.length} 条任务记录？`}
            description={
              <div>
                <div>删除后不可恢复。</div>
                {purge.checkbox}
              </div>
            }
            okText="删除"
            cancelText="取消"
            onConfirm={() => void batchRemove()}
            onOpenChange={(open) => {
              if (open) {
                purge.reset()
              }
            }}
          >
            <Button danger disabled={history.selectedRowKeys.length === 0}>
              批量删除{history.selectedRowKeys.length > 0 ? ` (${history.selectedRowKeys.length})` : ''}
            </Button>
          </Popconfirm>
        }
      />

      {/* ---------- 结果弹窗 ---------- */}
      <Modal
        open={results.jobId !== null}
        title={
          results.jobId !== null && (
            <Space size={8}>
              <EyeOutlined />
              <span>抓取结果</span>
              <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
                任务 #{results.jobId} · {results.notes.length} 条
              </Text>
            </Space>
          )
        }
        footer={null}
        // 这页结果表列最多（还带操作列的「换背景」），窄了标题列会被挤成一条缝；
        // antd 自己有 max-width: calc(100vw - 32px) 兜底，小屏会自动缩回来
        width={1400}
        onCancel={results.close}
        destroyOnHidden
      >
        <Table
          rowKey="index"
          loading={results.loading}
          dataSource={results.notes}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          scroll={{ x: 1080 }}
          expandable={{
            expandedRowRender: (note) => noteDetail(note, results.jobId),
            rowExpandable: (note) =>
              note.desc !== '' || note.images.length > 0 || note.local_videos.length > 0,
          }}
          columns={noteColumns(results.jobId, copyText, swapBackground)}
        />
      </Modal>

      {/* ---------- 日志尾部弹窗 ---------- */}
      <Modal
        open={results.logOpen}
        title="MC 日志尾部"
        footer={null}
        width={860}
        onCancel={results.closeLog}
        destroyOnHidden
      >
        {results.logLoading ? (
          <Flex justify="center" style={{ padding: 32 }}>
            <Spin />
          </Flex>
        ) : (
          <pre
            style={{
              maxHeight: '60vh',
              overflow: 'auto',
              padding: 12,
              background: 'var(--color-bg-soft, #f5f5f5)',
              borderRadius: 6,
              fontSize: 12,
              lineHeight: 1.7,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              margin: 0,
            }}
          >
            {results.logText || '（暂无日志）'}
          </pre>
        )}
      </Modal>

      {/* ---------- 存到 Cookie 库弹窗 ---------- */}
      <Modal
        open={cookieLib.saveOpen}
        title={`把当前 Cookie 存到「${PLATFORM_META[form.platform].label}」库`}
        okText="保存"
        cancelText="取消"
        confirmLoading={cookieLib.saving}
        onOk={() => void cookieLib.confirmSave(form.platform, form.cookies.trim())}
        onCancel={cookieLib.closeSave}
        destroyOnHidden
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          起个能认出来的名字（如「主号」「小号」）；同平台同名会覆盖旧值
        </Text>
        <Input
          value={cookieLib.saveName}
          onChange={(event) => cookieLib.setSaveName(event.target.value)}
          placeholder="例如：主号"
          maxLength={50}
          style={{ marginTop: 8 }}
        />
      </Modal>

      {/* ---------- Cookie 库管理弹窗 ---------- */}
      <CookieManagerModal
        open={cookieLib.managerOpen}
        items={cookieLib.items}
        loading={cookieLib.loading}
        onClose={() => cookieLib.setManagerOpen(false)}
        onEdit={(id) => void cookieLib.beginEdit(id)}
        onDelete={(id, name) => void cookieLib.remove(id, name)}
      />

      {/* ---------- Cookie 编辑弹窗（叠在管理弹窗上） ---------- */}
      {cookieLib.editing && (
        <CookieEditModal
          cookie={cookieLib.editing}
          onClose={cookieLib.closeEdit}
          onSaved={(updated) => {
            void cookieLib.applyEdit(updated)
            // 编辑的恰好是当前选中那条：输入框里的旧串一起换掉
            if (cookieLib.selectedId === updated.id) {
              form.setCookies(updated.cookie)
            }
          }}
        />
      )}

      {/* ---------- 备注编辑 ---------- */}
      {remark.editing && (
        <JobRemarkModal
          job={remark.editing}
          save={updateCrawlJobRemark}
          onClose={remark.close}
          onSaved={remark.handleSaved}
        />
      )}
    </div>
  )
}

/** 一条笔记的图片地址列表：本地已下载的优先（走 media 接口），没有的回退远程直链 */
function noteImages(jobId: number | null, note: CrawlNote): string[] {
  const remote =
    note.cover && !note.images.includes(note.cover) ? [note.cover, ...note.images] : note.images
  if (jobId === null) {
    return remote
  }
  const local = note.local_images.map((rel) => crawlMediaUrl(jobId, rel))
  const out: string[] = []
  for (let i = 0; i < Math.max(local.length, remote.length); i += 1) {
    out.push(local[i] ?? remote[i])
  }
  return out
}

/** 结果表列定义 */
function noteColumns(
  jobId: number | null,
  copy: (text: string) => void,
  swapBackground: (note: CrawlNote) => void,
): ColumnsType<CrawlNote> {
  return [
    { title: '#', dataIndex: 'index', width: 44 },
    {
      title: '封面',
      width: 72,
      render: (_: unknown, note: CrawlNote) => {
        const src = noteImages(jobId, note)[0]
        return src ? (
          // 远程图挂 no-referrer：小红书等平台的图有 Referer 防盗链
          <Image
            src={src}
            width={48}
            height={48}
            style={{ objectFit: 'cover', borderRadius: 6 }}
            referrerPolicy="no-referrer"
          />
        ) : (
          <Text type="secondary">—</Text>
        )
      },
    },
    {
      title: '标题',
      ellipsis: true,
      render: (_: unknown, note: CrawlNote) => (
        <Tooltip title={note.title || note.desc}>
          <span>{noteDisplayTitle(note)}</span>
        </Tooltip>
      ),
    },
    { title: '作者', dataIndex: 'nickname', width: 110, ellipsis: true },
    // 互动数三列各自可排序：MC 落盘是 "6.6万" 这类字符串，排序先经 parseCountValue
    // 转数值（纯字符串排序会把 "999" 排在 "6.6万" 前面）；首次点击即从高到低。
    ...(
      [
        { title: '点赞', field: 'liked_count' },
        { title: '收藏', field: 'collected_count' },
        { title: '评论', field: 'comment_count' },
      ] as const
    ).map(
      (column): ColumnsType<CrawlNote>[number] => ({
        title: column.title,
        dataIndex: column.field,
        width: 76,
        align: 'right',
        sortDirections: ['descend', 'ascend'],
        sorter: (a, b) => parseCountValue(a[column.field]) - parseCountValue(b[column.field]),
        render: (value: string) => (
          <Text type="secondary" style={{ fontSize: 12 }}>
            {value || '—'}
          </Text>
        ),
      }),
    ),
    {
      title: '发布',
      dataIndex: 'publish_time',
      width: 100,
      render: (value: string) => (value ? formatDateTime(value) : '—'),
    },
    {
      title: '图片',
      width: 64,
      render: (_: unknown, note: CrawlNote) =>
        note.images.length > 0 ? `${note.images.length} 张` : '—',
    },
    {
      title: '操作',
      width: 176,
      render: (_: unknown, note: CrawlNote) => {
        const imageCount = note.local_images.length
        return (
          <Space size={4}>
            {note.url && (
              <a href={note.url} target="_blank" rel="noreferrer">
                原文
              </a>
            )}
            {note.url && (
              <Button
                type="text"
                icon={<CopyOutlined />}
                onClick={() => copy(note.url)}
                title="复制链接"
              />
            )}
            {/* 一键换背景：把这条笔记已下载到本地的图整批带过去当原图。

                没图时按钮是灰的，而灰按钮（antd 6 用 pointer-events:none 关掉交互）
                自己不收鼠标事件，所以 Tooltip 挂在外层 span 上 —— 否则「灰了为什么」
                就没人回答 */}
            <Tooltip
              title={
                imageCount > 0
                  ? `把这条笔记的 ${imageCount} 张图带到「一键换背景」`
                  : '这条笔记没有下载到本地图片（媒体下载未开启，或该平台无法按笔记关联图片）'
              }
            >
              <span style={{ display: 'inline-block' }}>
                <Button
                  type="text"
                  icon={<BgColorsOutlined />}
                  disabled={imageCount === 0}
                  onClick={() => swapBackground(note)}
                >
                  换背景
                </Button>
              </span>
            </Tooltip>
          </Space>
        )
      },
    },
  ]
}

/** 结果表展开行：正文全文 + 图组预览 + 本地视频 */
function noteDetail(note: CrawlNote, jobId: number | null): ReactNode {
  const images = noteImages(jobId, note)
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {note.desc && (
        <Paragraph
          style={{ marginBottom: 0, whiteSpace: 'pre-wrap' }}
          ellipsis={{ rows: 4, expandable: true, symbol: '展开全文' }}
        >
          {note.desc}
        </Paragraph>
      )}
      {images.length > 0 && (
        <Image.PreviewGroup>
          <Space wrap size={8}>
            {images.map((src, i) => (
              <Image
                key={`${src}-${i}`}
                src={src}
                width={88}
                height={88}
                style={{ objectFit: 'cover', borderRadius: 6 }}
                referrerPolicy="no-referrer"
              />
            ))}
          </Space>
        </Image.PreviewGroup>
      )}
      {jobId !== null && note.local_videos.length > 0 && (
        <Space wrap size={8}>
          {note.local_videos.map((rel) => (
            <video
              key={rel}
              src={crawlMediaUrl(jobId, rel)}
              controls
              style={{ maxWidth: 320, maxHeight: 240, borderRadius: 6 }}
            />
          ))}
        </Space>
      )}
      {note.source_keyword && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          来源关键词：{note.source_keyword}
        </Text>
      )}
    </Space>
  )
}

/** 历史任务「内容」列：按模式取关键词 / 链接 / 创作者的摘要 */
function paramsSummary(job: CrawlJob): string {
  const { keywords, ids, creators } = job.params
  if (keywords && keywords.length > 0) {
    return keywords.join('、')
  }
  const list =
    ids && ids.length > 0 ? ids : creators && creators.length > 0 ? creators : []
  if (list.length === 0) {
    return '—'
  }
  const first = list[0].length > 48 ? `${list[0].slice(0, 48)}…` : list[0]
  return list.length > 1 ? `${first} 等 ${list.length} 条` : first
}

/** 当前任务阶段流程：紧凑标签流，当前阶段高亮，下面一行 hint 告诉用户该做什么 */
function PhaseSteps({ job }: { job: CrawlJob }) {
  const steps = phaseSteps(job.login_type)
  const currentPhase = job.phase as CrawlPhase
  const currentIndex =
    job.status === 'pending' ? -1 : Math.max(0, steps.indexOf(currentPhase))
  const activePhase =
    currentIndex >= 0 && currentIndex < steps.length ? steps[currentIndex] : null

  return (
    <div style={{ marginTop: 12 }}>
      <Flex align="center" gap={4} wrap>
        {steps.map((phase, i) => (
          <Flex key={phase} align="center" gap={4}>
            {i > 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                →
              </Text>
            )}
            {i === currentIndex ? (
              <Tag color="processing" style={{ marginInlineEnd: 0 }}>
                {PHASE_META[phase].label}
              </Tag>
            ) : (
              <Text
                type={i < currentIndex ? 'success' : 'secondary'}
                style={{ fontSize: 12 }}
                delete={false}
              >
                {i < currentIndex ? `✓ ${PHASE_META[phase].label}` : PHASE_META[phase].label}
              </Text>
            )}
          </Flex>
        ))}
      </Flex>
      {activePhase && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          {PHASE_META[activePhase].hint}
        </Text>
      )}
    </div>
  )
}

/** 历史任务表格列定义 */
function historyColumns(handlers: {
  onView: (jobId: number) => void
  onCancel: (jobId: number) => void
  onDelete: (jobId: number) => void
  onEditRemark: (job: CrawlJob) => void
  onRetry: (job: CrawlJob) => void
  purge: UsePurgeFilesResult
}): ColumnsType<CrawlJob> {
  return [
    jobIdColumn<CrawlJob>(),
    jobStatusColumn<CrawlJob>(JOB_STATUS_META),
    {
      title: '平台',
      dataIndex: 'platform',
      width: 96,
      render: (platform: CrawlPlatform) => (
        <Tag color={PLATFORM_META[platform].color}>{PLATFORM_META[platform].label}</Tag>
      ),
    },
    {
      title: '模式',
      dataIndex: 'crawler_type',
      width: 104,
      render: (type: CrawlerType) => CRAWLER_TYPE_META[type].label,
    },
    {
      title: '内容',
      ellipsis: true,
      render: (_: unknown, job: CrawlJob) => paramsSummary(job),
    },
    { title: '笔记', dataIndex: 'note_count', width: 64 },
    jobRemarkColumn<CrawlJob>({ onEdit: handlers.onEditRemark }),
    jobCreatedColumn<CrawlJob>(),
    jobActionsColumn<CrawlJob>({
      isTerminal: (job) => isTerminalStatus(job.status),
      onView: handlers.onView,
      onCancel: handlers.onCancel,
      onDelete: handlers.onDelete,
      deleteDescription: '删除后不可恢复。',
      purge: handlers.purge,
      // 抓取没有条目级状态，重试 = 按原参数另起一条新任务（新 id、新产物目录）。
      // 只有没跑出结果的才给点：跑成功的重跑一遍没有意义，跑着的后端也会 409 挡
      onRetry: handlers.onRetry,
      canRetry: (job) => job.status === 'failed' || job.status === 'cancelled',
    }),
  ]
}
