/**
 * 「从素材抓取选图」弹窗（本页私有）。
 *
 * 形态与 FinalCut 的「选择字幕产物」一致：选一个来源任务、再从它的产物里挑一个，
 * 只是这里的产物是「一条笔记的一批图」，所以下面那张表按笔记列。
 *
 * 只负责摆出来，两段式取数在私有 hook useCrawlSource 里。
 *
 * 一条笔记的图**整批**带过去（`pick`），不做跨笔记多选：换背景的输入模型是
 * 「一个原图目录 + 一批文件名」，而每条笔记的图各自在一个目录里 —— 跨笔记就得
 * 改后端输入模型，为此不值得。
 */

import { Button, Empty, Flex, Image, Modal, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { crawlMediaUrl } from '../../api/crawler'
import { PLATFORM_META, noteDisplayTitle } from '../../types/crawler'
import type { CrawlJob, CrawlNote } from '../../types/crawler'

const { Text } = Typography

interface CrawlSourceModalProps {
  open: boolean
  /** 可选的抓取任务（终态且有内容） */
  jobs: CrawlJob[]
  jobsLoading: boolean
  activeJobId: number | null
  /** 当前任务里有本地图片的笔记 */
  notes: CrawlNote[]
  notesLoading: boolean
  onSelectJob: (jobId: number) => void
  /** 选中一条笔记：整批图带到换背景的输入区 */
  onPick: (note: CrawlNote) => void
  onClose: () => void
}

export default function CrawlSourceModal({
  open,
  jobs,
  jobsLoading,
  activeJobId,
  notes,
  notesLoading,
  onSelectJob,
  onPick,
  onClose,
}: CrawlSourceModalProps) {
  return (
    <Modal
      open={open}
      title="从素材抓取选图"
      width={900}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
    >
      {jobsLoading ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <Spin />
        </div>
      ) : jobs.length === 0 ? (
        <Empty description="还没有抓到东西的任务。先去「素材抓取」跑一个，抓到图之后这里才选得出来。" />
      ) : (
        <Flex vertical gap={12}>
          <Space size={8}>
            <Text type="secondary">抓取任务</Text>
            <Select
              value={activeJobId ?? undefined}
              onChange={onSelectJob}
              style={{ width: 380 }}
              options={jobs.map((job) => ({
                value: job.id,
                label: `#${job.id} · ${PLATFORM_META[job.platform].label} · ${job.note_count} 条`,
              }))}
            />
          </Space>

          <Table<CrawlNote>
            // 同一条笔记在不同任务里 id 一样，行 key 得带上任务才唯一
            rowKey={(note) => `${activeJobId}-${note.id}`}
            dataSource={notes}
            loading={notesLoading}
            pagination={notes.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
            columns={noteColumns(activeJobId, onPick)}
            locale={{
              emptyText: <Empty description="这条任务里没有下载到本地的图片" />,
            }}
          />
        </Flex>
      )}
    </Modal>
  )
}

/**
 * 笔记表的列。
 *
 * 留在这个文件里、不提到页面上：列里用到的 `noteDisplayTitle` 与封面地址都只
 * 服务这一个弹窗，摆在一起改起来才不用两头找。
 */
function noteColumns(
  jobId: number | null,
  onPick: (note: CrawlNote) => void,
): ColumnsType<CrawlNote> {
  return [
    {
      title: '封面',
      key: 'cover',
      width: 72,
      render: (_value, note) => {
        // 只喂本地图（远端直链在小红书这类平台上会过期，缩略图时不稳）
        const first = note.local_images[0]
        if (jobId === null || first === undefined) {
          return <Text type="secondary">—</Text>
        }
        return (
          <Image
            src={crawlMediaUrl(jobId, first)}
            width={48}
            height={48}
            loading="lazy"
            style={{ objectFit: 'cover', borderRadius: 4 }}
          />
        )
      },
    },
    {
      title: '笔记',
      key: 'title',
      render: (_value, note) => (
        <Flex vertical style={{ minWidth: 0 }}>
          <Text ellipsis={{ tooltip: noteDisplayTitle(note) }}>{noteDisplayTitle(note)}</Text>
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {note.nickname || '—'}
            </Text>
            {/* 关键词是搜索模式下的来路标记，一条任务抓多个词时靠它分辨 */}
            {note.source_keyword ? (
              <Tag style={{ marginInlineEnd: 0 }}>{note.source_keyword}</Tag>
            ) : null}
          </Space>
        </Flex>
      ),
    },
    {
      title: '图片',
      key: 'images',
      width: 90,
      render: (_value, note) => `${note.local_images.length} 张`,
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_value, note) => (
        <Button type="link" onClick={() => onPick(note)}>
          选这批图
        </Button>
      ),
    },
  ]
}
