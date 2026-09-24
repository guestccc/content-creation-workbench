/**
 * 「评论」弹窗：看一条笔记抓到手的评论，以及在没抓到 / 抓得太少时补抓一次。
 *
 * 状态在页面侧的 useNoteComments 里（读取、轮询、发起补抓、取消都在那边），
 * 这里只负责把三种形态摆出来：列表、空态、补抓设置。
 *
 * **两个数字必须一起给**：「本地已抓 N 条」和「原文共 X 条」。结果表里那列
 * 「评论」显示的是平台说的总数（可能是 3286），点进来却只有几十条 —— 因为
 * MC 每条笔记最多抓 200 条一级评论，而且**原任务建的时候可能压根没开评论采集**。
 * 只给一个数，用户会当成坏了。
 *
 * **空态分三种，不能合并**（合并成一句「去抓评论」会让「抓过但这条确实没有」
 * 的用户反复点同一个按钮）：
 *
 *   1. 原任务没开评论采集 → 「这次抓取没有采集评论」+ 主按钮「去抓评论」
 *   2. 开了但这条一条没抓到 → 「这条笔记没抓到评论」+ 次要按钮「再抓一次」
 *   3. 有评论 → 列表
 *
 * **补抓在跑时状态条是唯一出口**：补抓任务不进历史任务列表（它是原任务的派生
 * 任务，混进列表只会让人以为抓了两次），所以「排在第几位 / 抓到了几条 / 失败
 * 原因」只在这里露脸。
 */

import { useState } from 'react'
import { CommentOutlined, ReloadOutlined } from '@ant-design/icons'
import {
  Alert,
  Button,
  Empty,
  Flex,
  Image,
  Input,
  List,
  Modal,
  Radio,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'

import { noteDisplayTitle } from '../../types/crawler'
import type {
  CrawlComment,
  CrawlCommentRefetchState,
  CrawlLoginType,
  CrawlNote,
  NoteCommentsData,
} from '../../types/crawler'
import { formatDateTime } from '../../utils/format'
import { crawlMediaUrl } from '../../api/crawler'
import { TOP_COMMENT_OPTIONS } from './useNoteComments'
import type { UseNoteCommentsResult } from './useNoteComments'
import type { useCookieLibrary } from './useCookieLibrary'

const { Text, Paragraph } = Typography

/** 一级评论每页显示多少条（子评论挂在父下面，跟着父一起翻页） */
const PAGE_SIZE = 20

/**
 * 评论图的两种形态：http(s) 是平台原图 URL（时效签名，过期即 403 —— 通常是
 * 没缓存成功的，裂图只能靠补抓刷新）；其余是本地缓存路径，走 /media 取。
 */
function pictureSrc(jobId: number, src: string): string {
  return src.startsWith('http') ? src : crawlMediaUrl(jobId, src)
}

interface CommentsModalProps {
  /** useNoteComments() 的返回值，原样传进来 */
  state: UseNoteCommentsResult
  /** Cookie 库（页面级 hook）：补抓设置的「从库选择 / 存到库 / 管理库」与建任务表单共用 */
  cookieLib: ReturnType<typeof useCookieLibrary>
}

export default function CommentsModal({ state, cookieLib }: CommentsModalProps) {
  const { target, visible, data, view } = state
  // 手动挡的兜底（state.target 为 null 时整个弹窗是空壳）：正常流程里两者同生共死
  const note = target?.note ?? null

  return (
    <Modal
      open={visible}
      title={
        note !== null && (
          <Space size={8}>
            <CommentOutlined />
            <span>评论</span>
            <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
              {noteDisplayTitle(note)}
            </Text>
          </Space>
        )
      }
      footer={note === null ? null : <ModalFooter state={state} note={note} data={data} />}
      width={900}
      // 评论动辄上百行（还带子评论），内容区自己滚，标题与底栏留外面
      styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
      onCancel={state.close}
      destroyOnHidden
    >
      {view === 'setup' ? (
        <SetupPanel state={state} data={data} cookieLib={cookieLib} />
      ) : (
        <CommentsPanel state={state} note={note} />
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------------------
// 视图 A：列表 / 空态
// ---------------------------------------------------------------------------

function CommentsPanel({ state, note }: { state: UseNoteCommentsResult; note: CrawlNote | null }) {
  const { data, loading, error, target } = state
  // 评论图缓存路径要按任务 id 拼地址；data 非 null 时 target 一定在（两者同生共死）
  const jobId = target?.jobId ?? 0

  if (data === null && loading) {
    return (
      <Flex justify="center" style={{ padding: 32 }}>
        <Spin />
      </Flex>
    )
  }

  return (
    <Flex vertical gap={12}>
      {/* 读失败摆在弹窗里（而不是 toast）：它要配一个「重试」 */}
      {error !== '' && (
        <Alert
          type="error"
          showIcon
          message="读取评论失败"
          description={<Text type="secondary">{error}</Text>}
          action={
            <Button size="small" onClick={state.refresh}>
              重试
            </Button>
          }
        />
      )}

      {data !== null && (
        <>
          <RefetchBanner
            refetch={data.refetch}
            onCancel={state.cancelRefetch}
            onRetry={state.showSetup}
          />

          {data.comments.length === 0 ? (
            <EmptyState state={state} data={data} />
          ) : (
            <List
              rowKey="id"
              size="small"
              dataSource={data.comments}
              pagination={
                data.comments.length > PAGE_SIZE
                  ? { pageSize: PAGE_SIZE, size: 'small', showSizeChanger: false }
                  : false
              }
              renderItem={(comment) => (
                <CommentItem comment={comment} depth={0} jobId={jobId} />
              )}
            />
          )}

          {note !== null && <ShadowNote note={note} data={data} />}
        </>
      )}
    </Flex>
  )
}

/**
 * 结果表里那列「评论」与这里实际抓到的差额说明。
 *
 * 不写这一句，用户看到列表里几十条、表里写着 3286，第一反应是功能坏了。
 */
function ShadowNote({ note, data }: { note: CrawlNote; data: NoteCommentsData }) {
  const origin = note.comment_count ? `原文共 ${note.comment_count} 条` : '原文条数未知'
  const local = data.comments_config.enabled
    ? `本地已抓 ${data.total} 条（一级上限 ${data.comments_config.max_comments}）`
    : `本地已抓 ${data.total} 条`
  return (
    <Text type="secondary" style={{ fontSize: 12 }}>
      {local} · {origin}；平台上的评论可能远多于抓到的（受条数上限与风控影响），
      补抓只补这一条笔记。
    </Text>
  )
}

/** 补抓任务的状态条；从没补抓过（refetch 为 null）时整块不渲染 */
function RefetchBanner({
  refetch,
  onCancel,
  onRetry,
}: {
  refetch: CrawlCommentRefetchState | null
  onCancel: () => void
  onRetry: () => void
}) {
  if (refetch === null) {
    return null
  }

  /** 排队 / 抓取中：都给「取消」——它是一条真实任务，能停 */
  const cancelAction = (
    <Button size="small" danger onClick={onCancel}>
      取消
    </Button>
  )
  const retryAction = (
    <Button size="small" onClick={onRetry}>
      再抓一次
    </Button>
  )

  if (refetch.status === 'pending') {
    return (
      <Alert
        type="info"
        showIcon
        message="补抓任务在排队"
        description={
          <Text type="secondary">
            同时只跑一个抓取任务；排在它前面的还有 {refetch.queued_ahead} 个，轮到了会自动开始。
          </Text>
        }
        action={cancelAction}
      />
    )
  }
  if (refetch.status === 'running') {
    return (
      <Alert
        type="info"
        showIcon
        icon={<Spin size="small" />}
        message="正在补抓这条笔记的评论"
        description={
          <Text type="secondary">
            可能会弹出浏览器窗口（扫码登录的任务需要现场扫码）；抓完这里的列表会自己更新。
          </Text>
        }
        action={cancelAction}
      />
    )
  }
  if (refetch.status === 'failed') {
    return (
      <Alert
        type="error"
        showIcon
        message="上次补抓失败"
        description={
          <Text type="secondary">
            {refetch.error_message || '原因见任务日志；小红书链接的访问令牌会过期，重新搜索该笔记后再试通常可行。'}
          </Text>
        }
        action={retryAction}
      />
    )
  }
  if (refetch.status === 'cancelled') {
    return (
      <Alert type="warning" showIcon message="上次补抓已取消" action={retryAction} />
    )
  }
  return (
    <Alert
      type="success"
      showIcon
      message={`上次补抓新增 ${refetch.comment_count} 条评论`}
      action={retryAction}
    />
  )
}

/** 空态：三种情况的话术与主按钮都不同（见文件头注释） */
function EmptyState({ state, data }: { state: UseNoteCommentsResult; data: NoteCommentsData }) {
  const neverCollected = !data.comments_config.enabled
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <Space direction="vertical" size={4}>
          <Text>{neverCollected ? '这次抓取没有采集评论' : '这条笔记没抓到评论'}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {neverCollected
              ? '原任务创建时关掉了「抓评论」，或者把条数设成了 0，所以产物里没有评论。可以对这条笔记单独补抓一次。'
              : '可能确实没有评论，也可能评论已删除或已关闭。可以再抓一次试试。'}
          </Text>
        </Space>
      }
    >
      {neverCollected ? (
        <Button type="primary" onClick={state.showSetup} disabled={state.refetchLive}>
          去抓评论
        </Button>
      ) : (
        <Button icon={<ReloadOutlined />} onClick={state.showSetup} disabled={state.refetchLive}>
          再抓一次
        </Button>
      )}
    </Empty>
  )
}

/** 一条评论：作者 / 时间 / 点赞 + 正文 + 图，子评论缩进挂在下面 */
function CommentItem({
  comment,
  depth,
  jobId,
}: {
  comment: CrawlComment
  depth: number
  /** 图片缓存路径拼 /media 地址用（见 pictureSrc） */
  jobId: number
}) {
  // sub_comment_count 是平台说的子评论总数，children 是实际抓到的 ——
  // 差额要显式说出来，否则用户以为这条线程是完整的
  const missing = Math.max(0, comment.sub_comment_count - comment.children.length)
  return (
    <div style={{ paddingLeft: depth * 24, marginBottom: 12 }}>
      <Flex justify="space-between" align="flex-start" gap={8}>
        <Space size={8} wrap>
          <Text strong style={{ fontSize: 13 }}>
            {comment.nickname || '匿名用户'}
          </Text>
          {/* 父评论没抓到（截断或风控）时它被提成一级展示，必须标明 —— 不标
              就等于在编造一条不存在的一级评论 */}
          {comment.orphan && (
            <Tooltip title="这条评论原本是对某条回复的回复，但那条父评论没抓到（被条数上限截断或被风控），所以提到了这里">
              <Tag color="orange" style={{ marginInlineEnd: 0, fontSize: 11 }}>
                父评论未抓到
              </Tag>
            </Tooltip>
          )}
        </Space>
        <Space size={8} style={{ flexShrink: 0 }}>
          {comment.created_at && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {formatDateTime(comment.created_at)}
            </Text>
          )}
          {/* 空串 = 这平台不落盘点赞，不是 0 赞，所以整个不渲染 */}
          {comment.liked_count !== '' && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              👍 {comment.liked_count}
            </Text>
          )}
        </Space>
      </Flex>

      <Paragraph style={{ margin: '4px 0 0', whiteSpace: 'pre-wrap' }}>
        {comment.content || '（空评论）'}
      </Paragraph>

      {comment.pictures.length > 0 && (
        <Space wrap size={8} style={{ marginTop: 4 }}>
          {comment.pictures.map((src) => (
            <Image
              key={src}
              src={pictureSrc(jobId, src)}
              width={64}
              height={64}
              style={{ objectFit: 'cover', borderRadius: 6 }}
              // 平台图有 Referer 防盗链（本地缓存路径不受影响，属性留着无妨）
              referrerPolicy="no-referrer"
            />
          ))}
        </Space>
      )}

      {missing > 0 && (
        <div style={{ marginTop: 4 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            还有 {missing} 条回复未抓到
          </Text>
        </div>
      )}

      {comment.children.map((child) => (
        <div key={child.id} style={{ marginTop: 8 }}>
          <CommentItem comment={child} depth={depth + 1} jobId={jobId} />
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// 视图 B：补抓设置
// ---------------------------------------------------------------------------

function SetupPanel({
  state,
  data,
  cookieLib,
}: {
  state: UseNoteCommentsResult
  data: NoteCommentsData | null
  cookieLib: ReturnType<typeof useCookieLibrary>
}) {
  // 选择框只是插入器（选中即回填下方输入框），选中态留在本视图内即可：
  // 切回列表视图会卸载这里，再次进来重新选 —— 与建任务表单同口径
  const [selectedCookieId, setSelectedCookieId] = useState<number | null>(null)
  if (data === null) {
    // 设置视图的入口（空态按钮 / 状态条 / 底栏）都要求评论数据已加载，
    // 走到这里等于还没拿到数据 —— 不渲染，等数据到了再说
    return null
  }
  const libraryCookies = cookieLib.cookiesForPlatform(data.platform)

  return (
    <Flex vertical gap={16}>
      <Alert
        type="info"
        showIcon
        message="补抓会打开一个真实的浏览器窗口"
        description={
          <Text type="secondary">
            登录方式与无头默认沿用原任务，可以在这里改；选扫码登录时需要现场扫码确认。
            任务是排队执行的，前面还有任务时要等它跑完；已抓到的评论会保留，重复的以补抓到的为准。
          </Text>
        }
      />

      {data.total > 0 && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          这条笔记本地已有 {data.total} 条评论，补抓的结果会并进来。
        </Text>
      )}

      <div>
        <Text>一级评论条数</Text>
        <div style={{ marginTop: 4 }}>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={state.maxComments}
            onChange={(event) => state.setMaxComments(Number(event.target.value))}
            options={TOP_COMMENT_OPTIONS.map((count) => ({ value: count, label: `${count} 条` }))}
          />
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          单条笔记最多抓这么多条一级评论（平台一般最多 200 条）
        </Text>
      </div>

      <Flex justify="space-between" align="flex-start" gap={16}>
        <div>
          <Text>同时抓二级评论</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              回复里常有真实使用反馈，带货选品建议开着
            </Text>
          </div>
        </div>
        <Switch checked={state.subComments} onChange={state.setSubComments} />
      </Flex>

      <div>
        <Text>登录方式</Text>
        <div style={{ marginTop: 4 }}>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={state.loginType}
            onChange={(event) => state.setLoginType(event.target.value as CrawlLoginType)}
          >
            <Radio.Button value="qrcode">扫码登录</Radio.Button>
            <Radio.Button value="cookie">Cookie 登录</Radio.Button>
          </Radio.Group>
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {state.loginType === 'qrcode'
            ? '二维码走系统看图软件弹出，与无头无关；Cookie 过期导致失败时换成扫码通常就好了'
            : '用一串 Cookie 直接登录，适合挂机；串过期是补抓失败最常见的原因'}
        </Text>
      </div>

      {state.loginType === 'cookie' && (
        <div>
          <Flex gap={8} align="center">
            <Select
              value={selectedCookieId}
              onChange={(id) => {
                if (id === undefined || id === null) {
                  setSelectedCookieId(null)
                  return
                }
                setSelectedCookieId(id)
                state.selectCookie(id)
              }}
              style={{ flex: 1 }}
              placeholder={
                libraryCookies.length > 0
                  ? '从 Cookie 库选择（选中即回填下方）'
                  : 'Cookie 库还是空的，先粘贴再「存到库」'
              }
              allowClear
              disabled={libraryCookies.length === 0}
              options={libraryCookies.map((item) => ({
                value: item.id,
                label: item.remark ? `${item.name}（${item.remark}）` : item.name,
              }))}
            />
            <Button
              onClick={() => cookieLib.openSave(data.platform, state.cookies.trim())}
              disabled={!state.cookies.trim()}
            >
              存到库
            </Button>
            {cookieLib.items.length > 0 && (
              <Button onClick={() => cookieLib.setManagerOpen(true)}>管理库</Button>
            )}
          </Flex>
          <Input.TextArea
            value={state.cookies}
            onChange={(event) => state.setCookies(event.target.value)}
            rows={2}
            placeholder="留空则沿用原任务存的 Cookie；刚重新登录过，就把新的 Cookie 串贴到这里"
            style={{ marginTop: 8 }}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            只保存在本机数据库里，不会出现在任何接口返回中；存到库后下次直接选，同名会覆盖
          </Text>
        </div>
      )}

      <Flex justify="space-between" align="flex-start" gap={16}>
        <div>
          <Text>无头模式</Text>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              开启后抓取全程不显示浏览器窗口；若遇到滑块等验证，请关掉无头后重试
            </Text>
          </div>
        </div>
        <Switch checked={state.headless} onChange={state.setHeadless} />
      </Flex>
    </Flex>
  )
}

// ---------------------------------------------------------------------------
// 底栏
// ---------------------------------------------------------------------------

function ModalFooter({
  state,
  note,
  data,
}: {
  state: UseNoteCommentsResult
  note: CrawlNote
  data: NoteCommentsData | null
}) {
  if (state.view === 'setup') {
    return (
      <Flex justify="space-between" align="center" gap={8}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          抓取期间可以关掉这个窗口，跑完了会提示
        </Text>
        <Space>
          <Button onClick={state.backToList} disabled={state.submitting}>
            返回
          </Button>
          <Button type="primary" loading={state.submitting} onClick={state.startRefetch}>
            开始抓取
          </Button>
        </Space>
      </Flex>
    )
  }

  return (
    <Flex justify="space-between" align="center" gap={8}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {note.comment_count ? `原文共 ${note.comment_count} 条评论` : ''}
      </Text>
      <Space>
        <Button onClick={state.close}>关闭</Button>
        {/* 补抓在跑时不给入口：点了只会 409（后端同一条笔记只允许一个在跑的补抓） */}
        <Button
          type="primary"
          onClick={state.showSetup}
          disabled={state.refetchLive || data === null}
        >
          补抓评论
        </Button>
      </Space>
    </Flex>
  )
}
