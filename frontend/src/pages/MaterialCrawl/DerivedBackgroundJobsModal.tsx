/**
 * 「这条笔记派生出来的换背景任务」简表（本页私有）。
 *
 * 抓取这边只给一张简表 + 一个「看详情」：换背景的逐图明细、参数、诊断统计都在
 * 换背景页的详情弹窗里有现成的，这里再铺一遍等于抄第二份（那份还更容易过期）。
 * 「看详情」跳到换背景页并直接打开那条任务的弹窗（走 BackgroundSwapEntry.openJobId）。
 *
 * 取数在页面侧的 useDerivedBackgroundJobs 里，这里只负责摆出来。
 *
 * ⚠️ 状态列用的是 **types/background** 的 JOB_STATUS_META，不是 types/crawler 里
 * 同名的那个：换背景有 `partial`（部分成功）这个抓取域没有的状态，拿抓取那份来查
 * 会在渲染到那条任务时直接崩。两个文件重名，导入时务必看准路径。
 */

import { Alert, Button, Empty, Flex, Modal, Space, Table, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'

import { jobCreatedColumn, jobIdColumn, jobStatusColumn } from '../../components/jobColumns'
import { JOB_STATUS_META } from '../../types/background'
import type { BackgroundJob } from '../../types/background'
import { noteDisplayTitle } from '../../types/crawler'
import type { CrawlNote } from '../../types/crawler'

const { Text } = Typography

interface DerivedBackgroundJobsModalProps {
  /** 正在看哪条笔记；null 表示弹窗关着 */
  note: CrawlNote | null
  /** 这条笔记派生出的换背景任务（新的在前） */
  jobs: BackgroundJob[]
  loading: boolean
  /** 查询失败的原因；有值就不能把空列表当成「没有换过背景」 */
  error: string | null
  onRefresh: () => void
  /** 「看详情」：跳到换背景页并打开那条任务 */
  onView: (jobId: number) => void
  onClose: () => void
}

export default function DerivedBackgroundJobsModal({
  note,
  jobs,
  loading,
  error,
  onRefresh,
  onView,
  onClose,
}: DerivedBackgroundJobsModalProps) {
  return (
    <Modal
      open={note !== null}
      title={
        note !== null && (
          <Space size={8}>
            <span>换背景任务</span>
            <Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
              {noteDisplayTitle(note)}
            </Text>
          </Space>
        )
      }
      footer={null}
      width={900}
      onCancel={onClose}
      destroyOnHidden
    >
      <Flex vertical gap={12}>
        {error !== null && (
          <Alert
            type="error"
            showIcon
            message="读取换背景任务失败"
            description={error}
            action={
              <Button size="small" onClick={onRefresh}>
                重试
              </Button>
            }
          />
        )}

        <Table<BackgroundJob>
          rowKey="id"
          dataSource={jobs}
          loading={loading}
          pagination={jobs.length > 10 ? { pageSize: 10, showSizeChanger: false } : false}
          columns={columns(onView)}
          locale={{
            // 查失败和确实一条都没有是两回事：前者别再说「还没有换过背景」
            // —— 那是在替后端下结论，用户会以为这条笔记真的没换过
            emptyText: (
              <Empty
                description={
                  error !== null ? '暂时读不到换背景任务（见上方提示）' : '这条笔记还没有换过背景'
                }
              />
            ),
          }}
        />
      </Flex>
    </Modal>
  )
}

/** 简表的列：只要能认出「哪条、跑到什么程度、产出了几张」 */
function columns(onView: (jobId: number) => void): ColumnsType<BackgroundJob> {
  return [
    jobIdColumn<BackgroundJob>(),
    jobStatusColumn<BackgroundJob>(JOB_STATUS_META),
    { title: '图片', dataIndex: 'total_images', width: 72 },
    {
      title: '失败',
      dataIndex: 'failed_images',
      width: 72,
      render: (value: number) =>
        value > 0 ? <Text type="danger">{value}</Text> : <Text type="secondary">0</Text>,
    },
    {
      title: '背景',
      dataIndex: 'background_name',
      width: 160,
      ellipsis: true,
      render: (value: string, job: BackgroundJob) => (
        <Tooltip title={job.background_path}>
          <Text style={{ fontSize: 12 }}>{value}</Text>
        </Tooltip>
      ),
    },
    jobCreatedColumn<BackgroundJob>(),
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_value, job) => (
        <Button type="link" style={{ padding: 0 }} onClick={() => onView(job.id)}>
          看详情
        </Button>
      ),
    },
  ]
}
