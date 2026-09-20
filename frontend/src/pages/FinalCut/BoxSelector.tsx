/**
 * 在视频画面上框选文案位置的组件。
 *
 * 手势交给两个成熟轮子，自己只做坐标换算：
 * - react-selecto：在画面上拖出一块区域 = 画框（`e.rect` 是相对 overlay 的
 *   容器坐标，overlay 无滚动，直接除以容器尺寸就归一化）；
 * - react-moveable：框的拖动与四角缩放，自带边界钳制与吸附对齐线
 *   （视频中线 / 四边各一条参考线，居中与贴边都有吸附）。
 *
 * 坐标闭环：`box` 归一化 0-1 相对视频显示尺寸；渲染时乘 overlay 的实测
 * 像素尺寸，提交时除以它。`<video>` 用 display:block + width:100%，高度
 * 自适应，overlay 与画面区天然重合，没有 letterbox 映射问题。
 *
 * 框内实时预览排版：用与后端同一套估算（typography.ts）算字号与折行，
 * 字号按 overlay 高 / 视频高 等比缩放到 CSS 像素 —— 近似所见即所得。
 */

import { useMemo, useRef, useState } from 'react'
import { PauseOutlined, PlayCircleOutlined } from '@ant-design/icons'
import { Button } from 'antd'
import Moveable from 'react-moveable'
import Selecto from 'react-selecto'

import type { BoxSpec } from '../../types/finalcut'
import { fitFontSize, wrapText } from './typography'

/** 框内预览的配色（由父组件从环境自检的 text_styles 解析后传入） */
export interface BoxPreviewStyle {
  textColor: string
  /** 底色（无底样式为透明） */
  background: string
}

interface BoxSelectorProps {
  /** 视频流地址（本地文件走 /fs/preview，历史产物走混剪的成片流） */
  src: string
  /** 视频切换时重建 <video> */
  videoKey?: string
  /** 当前框（归一化） */
  box: BoxSpec
  onBoxChange: (box: BoxSpec) => void
  /** 框内预览的文案 */
  text: string
  /** 0 = 自动字号 */
  fontSize: number
  previewStyle: BoxPreviewStyle
}

/** 归一化数保留 4 位小数（千分之一像素的精度没有意义，省得长尾漂移） */
function round4(value: number): number {
  return Math.round(value * 10000) / 10000
}

export default function BoxSelector({
  src,
  videoKey,
  box,
  onBoxChange,
  text,
  fontSize,
  previewStyle,
}: BoxSelectorProps) {
  // selecto/moveable 都要拿到真实的 overlay 元素，用回调 ref 落 state
  const [overlayEl, setOverlayEl] = useState<HTMLDivElement | null>(null)
  const [boxEl, setBoxEl] = useState<HTMLDivElement | null>(null)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [playing, setPlaying] = useState(false)
  /** overlay 的实测像素尺寸（归一化 ↔ 像素 换算的尺子） */
  const [size, setSize] = useState({ width: 0, height: 0 })
  /** 视频显示尺寸（排版估算要按真实分辨率算，再缩放回 CSS 像素） */
  const [videoDims, setVideoDims] = useState({ width: 0, height: 0 })

  const syncSize = () => {
    const el = overlayEl
    const video = videoRef.current
    if (el) {
      const rect = el.getBoundingClientRect()
      if (rect.width > 0 && rect.height > 0) {
        setSize({ width: rect.width, height: rect.height })
      }
    }
    if (video && video.videoWidth > 0) {
      setVideoDims({ width: video.videoWidth, height: video.videoHeight })
    }
  }

  const px = {
    x: box.x * size.width,
    y: box.y * size.height,
    w: box.w * size.width,
    h: box.h * size.height,
  }

  /** 把 overlay 容器内的一块像素矩形归一化并提交 */
  const commitRect = (left: number, top: number, width: number, height: number) => {
    if (size.width <= 0 || size.height <= 0) {
      return
    }
    const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
    const x = clamp01(left / size.width)
    const y = clamp01(top / size.height)
    onBoxChange({
      x: round4(x),
      y: round4(y),
      w: round4(Math.min(1 - x, width / size.width)),
      h: round4(Math.min(1 - y, height / size.height)),
    })
  }

  /** 拖动/缩放结束：从 DOM 实测矩形提交（transform 也算在内），并清掉 transform */
  const commitFromDom = (target: HTMLElement) => {
    if (!overlayEl) {
      return
    }
    const rect = target.getBoundingClientRect()
    const origin = overlayEl.getBoundingClientRect()
    commitRect(rect.left - origin.left, rect.top - origin.top, rect.width, rect.height)
    // React 不管 moveable 直写的 transform，提交后必须手动清掉，否则样式
    // left/top 更新后旧 transform 还叠着，框会「跳」一下
    target.style.transform = ''
  }

  // 框内实时预览：按视频分辨率估算字号与折行，再缩放到 overlay 的 CSS 像素
  const preview = useMemo(() => {
    if (videoDims.width <= 0 || videoDims.height <= 0 || size.height <= 0) {
      return null
    }
    const boxW = box.w * videoDims.width
    const boxH = box.h * videoDims.height
    const scale = size.height / videoDims.height
    if (fontSize > 0) {
      return {
        lines: wrapText(text, boxW, fontSize),
        fontSize: fontSize * scale,
        lineSpacing: 8 * scale,
      }
    }
    const fit = fitFontSize(text, boxW, boxH)
    return { lines: fit.lines, fontSize: fit.fontSize * scale, lineSpacing: 8 * scale }
  }, [text, box.w, box.h, fontSize, videoDims, size.height])

  return (
    <div style={{ position: 'relative', width: '100%' }}>
      <video
        key={videoKey}
        ref={videoRef}
        src={src}
        preload="metadata"
        style={{ display: 'block', width: '100%', background: '#000', borderRadius: 6 }}
        onLoadedMetadata={syncSize}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
      />
      {/* 框选层：盖住整个画面，selecto 在上面拖出矩形 */}
      <div
        ref={(el) => {
          setOverlayEl(el)
        }}
        style={{ position: 'absolute', inset: 0 }}
        onPointerDown={syncSize}
      >
        <div
          ref={setBoxEl}
          className="fc-box"
          style={{
            position: 'absolute',
            left: px.x,
            top: px.y,
            width: px.w,
            height: px.h,
            border: '1.5px dashed var(--ant-color-primary, #1677ff)',
            background: 'rgba(22, 119, 255, 0.08)',
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'center',
            overflow: 'hidden',
            cursor: 'move',
          }}
        >
          {/* 排版预览：穿透指针，画框/拖框不被文字挡住 */}
          {preview && (
            <div style={{ pointerEvents: 'none', padding: preview.fontSize * 0.2 }}>
              {preview.lines.map((line, index) => (
                <div
                  key={index}
                  style={{
                    textAlign: 'center',
                    color: previewStyle.textColor,
                    background: previewStyle.background,
                    fontSize: preview.fontSize,
                    lineHeight: `${preview.fontSize + preview.lineSpacing}px`,
                    fontWeight: 600,
                    // 描边近似：四向投影（烧进画面的是 drawtext 的真描边）
                    textShadow: '0 0 2px #000, 1px 1px 1px #000, -1px -1px 1px #000',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {line}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 画框：拖到已存在的框上时停手（那是移动/缩放，交给 moveable） */}
      {overlayEl && (
        <Selecto
          container={overlayEl}
          dragContainer={overlayEl}
          selectableTargets={['.fc-box-placeholder']}
          hitRate={0}
          selectByClick={false}
          selectFromInside={false}
          ratio={0}
          onDragStart={(e) => {
            const target = e.inputEvent?.target as HTMLElement | undefined
            if (target?.closest('.fc-box')) {
              e.stop()
            }
          }}
          onDragEnd={(e) => {
            commitRect(e.rect.left, e.rect.top, e.rect.width, e.rect.height)
          }}
        />
      )}

      {/* 拖动 / 缩放：边界钳在画面内，中线与四边吸附 */}
      {overlayEl && boxEl && size.width > 0 && (
        <Moveable
          target={boxEl}
          container={overlayEl}
          draggable
          resizable
          snappable
          snapThreshold={6}
          verticalGuidelines={[0, size.width / 2, size.width]}
          horizontalGuidelines={[0, size.height / 2, size.height]}
          bounds={{ left: 0, top: 0, right: size.width, bottom: size.height }}
          origin={false}
          onDrag={(e) => {
            e.target.style.transform = e.transform
          }}
          onDragEnd={(e) => commitFromDom(e.target as HTMLElement)}
          onResize={(e) => {
            e.target.style.width = `${e.width}px`
            e.target.style.height = `${e.height}px`
            e.target.style.transform = e.drag.transform
          }}
          onResizeEnd={(e) => commitFromDom(e.target as HTMLElement)}
        />
      )}

      {/* overlay 盖住了原生控制条，播放/暂停给一个浮动按钮 */}
      <Button
        size="small"
        type="text"
        icon={playing ? <PauseOutlined /> : <PlayCircleOutlined />}
        style={{ position: 'absolute', right: 8, bottom: 8, color: '#fff', zIndex: 2 }}
        onClick={() => {
          const video = videoRef.current
          if (!video) {
            return
          }
          if (video.paused) {
            void video.play()
          } else {
            video.pause()
          }
        }}
      />
    </div>
  )
}
