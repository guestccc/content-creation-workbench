/**
 * 历史任务「查看」弹窗：这条任务的参数、进度与逐张明细。
 *
 * 与 SceneSplit / SubtitleExtract 的历史「查看」同一套做法 —— 历史列表点开的是
 * **一条已经不在跑的任务**，把它挂到页面上方那张「当前任务」卡片里会顶掉正在跑的
 * 那条（进度卡、轮询都跟着换人），所以一律弹窗看，关掉就回到原样。
 *
 * 只有本页用得到，按项目约定留在页面自己的文件夹里。
 */

import { Alert, Descriptions, Flex, Modal, Space, Spin, Tag, Typography } from 'antd'

import { JOB_STATUS_META } from '../../types/background'
import type { BackgroundJob, BackgroundJobItem } from '../../types/background'
import { formatDateTime } from '../../utils/format'
import JobItemsTable from './JobItemsTable'

const { Text } = Typography

export default function JobDetailModal({
  jobId,
  job,
  loading,
  onClose,
  onRetryItem,
  onRetryAll,
}: {
  /** 正在查看的任务 id；null 表示弹窗关着 */
  jobId: number | null
  /** 读回来的任务详情；还在读的时候是 null */
  job: BackgroundJob | null
  loading: boolean
  onClose: () => void
  /** 弹窗里也能补跑：明细表的重试按钮透传给父页面处理刷新 */
  onRetryItem: (item: BackgroundJobItem) => void
  onRetryAll: (job: BackgroundJob) => void
}) {
  return (
    <Modal
      open={jobId !== null}
      title={
        jobId !== null && (
          <Space size={8}>
            <span>换背景任务 #{jobId}</span>
            {job && (
              <Tag color={JOB_STATUS_META[job.status].color}>
                {JOB_STATUS_META[job.status].label}
              </Tag>
            )}
          </Space>
        )
      }
      footer={null}
      width={1120}
      onCancel={onClose}
      destroyOnHidden
    >
      {loading && !job ? (
        <Flex justify="center" style={{ padding: 32 }}>
          <Spin />
        </Flex>
      ) : (
        job && (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Descriptions column={{ xs: 1, sm: 2 }} size="small">
              <Descriptions.Item label="原图目录" span={2}>
                <Text style={{ fontSize: 12 }}>{job.input_path}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="背景图">
                <Text style={{ fontSize: 12 }}>{job.background_name || '—'}</Text>
              </Descriptions.Item>
              <Descriptions.Item label="图片">
                {job.completed_images} / {job.total_images} 张完成
                {job.failed_images > 0 && ` · ${job.failed_images} 张失败`}
                {job.skipped_images > 0 && ` · ${job.skipped_images} 张跳过`}
              </Descriptions.Item>
              {/* 来源：素材抓取那边「看详情」跳过来时，「我为什么在这个页面」
                  得在这里自明。弱关联，来源任务被删了这行照样在 */}
              {job.source_crawl_job_id !== null && (
                <Descriptions.Item label="来源" span={2}>
                  素材抓取任务 #{job.source_crawl_job_id}
                  {job.source_crawl_note_id && (
                    <Text type="secondary" style={{ marginLeft: 8, fontSize: 12 }}>
                      笔记 {job.source_crawl_note_id}
                    </Text>
                  )}
                </Descriptions.Item>
              )}
              <Descriptions.Item label="输出目录" span={2}>
                <Text style={{ fontSize: 12 }}>{job.output_dir}</Text>
              </Descriptions.Item>
              {job.remark && <Descriptions.Item label="备注">{job.remark}</Descriptions.Item>}
              <Descriptions.Item label="创建于">{formatDateTime(job.created_at)}</Descriptions.Item>
            </Descriptions>

            {job.error_message && (
              <Alert type="error" showIcon message="失败原因" description={job.error_message} />
            )}

            <JobItemsTable job={job} onRetryItem={onRetryItem} onRetryAll={onRetryAll} />
          </Space>
        )
      )}
    </Modal>
  )
}
