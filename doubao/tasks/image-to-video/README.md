# 图片转视频任务

> 这是一个**持续型任务**。换会话时，让新会话读本文件 + `image-to-video-progress.json` 即可接着干，无需重新交代背景。

## 任务目标

把 `doubao/images/` 下的图片逐张生成视频，输出到 `doubao/videos/`，并记录每张图片对应的视频文件。每次执行都要跳过已完成的图片，只处理新增或未完成的。

## 目录结构

| 目录 | 作用 |
| --- | --- |
| `doubao/images/` | 源图片 |
| `doubao/videos/` | 生成的视频产物 |
| `doubao/tasks/` | 本任务描述与进度跟踪（本目录） |
| `doubao/specs/` | 提示词规范、风格参考（如《儿童科普图风格规范.md》） |

## 执行规则

> **权威规则见独立文档：[`video-generation-rules.md`](./video-generation-rules.md)**。每次执行必读该文件，本节不再重复。
> 要点速览：无音频；9:16；**镜头固定不动，只让中央内容主体微动**（背景/标题/文字标签/卡片全部静止）；命名 `产品名 YYYY-MM-DD.mp4`；5 秒（目标 3 秒，工具下限 5 秒）。

## 每次执行流程（必须遵守）

> **核心前提**：images/ 里的图片由其他会话持续生产，**本会话开始时永远不能假设 progress.json 是最新的**。每次执行第一步必须重新扫描图片目录。

1. **扫描 images/**：列出 `doubao/images/` 下所有图片文件（忽略 `.gitkeep` 和 `_*.md` 等标记文件），这是当前真实的待处理全集。
2. **对账同步**：与 `image-to-video-progress.json` 逐项比对：
   - images 里有、progress.json 里**没有**的 → 追加为 `pending`（新图，其他会话刚产的）。
   - progress.json 里有、但 images 里**已不存在**的 → 标注 `source_missing`，保留记录但不处理。
   - images 里有、progress.json 里 `status=done` 且对应视频仍在 videos/ 的 → **直接跳过**。
   - images 里有、progress.json 里 `status=done` 但对应视频文件丢了 → 改回 `pending` 重新生成。
3. 输出对账简报：本次新增几张、跳过几张、待处理几张。
4. 按"执行规则"逐张生成视频，输出到 `doubao/videos/`。
5. 生成成功后：
   - 更新 `image-to-video-progress.json`：写入 video 文件名、status=done、done_at 日期。
   - 更新 `进度总览.md` 和本文件下方"当前进度"表。
6. 生成失败：在 progress.json 里把该项标为 `status=error`，写明 error 原因，**不要**标记为 done，也不要删除已产出的半成品。

## 进度源文件

- `image-to-video-progress.json`：机器可读的**权威进度状态**，agent 每次读写它。
- `进度总览.md`：给人看的可视化进度表（已完成/待处理/遗留）。
- 本文件下方的进度表：概览。

## 当前进度（2026-09-19 初始化）

依据文件时间戳推断的历史映射：

| 图片 | 对应视频 | 状态 | 备注 |
| --- | --- | --- | --- |
| 扶梯.jpg | 自动扶梯01.mp4 | done | 扶梯=自动扶梯，01 后缀 |
| 空调.jpg | 空调.mp4 | done | |
| 自行车.jpeg | 自行车.mp4 | done | |
| 高铁.jpeg | 高铁.mp4 | done | |
| 相机01.jpeg | — | pending | |
| 相机02.jpeg | — | pending | |
| 相机03.jpeg | — | pending | |
| 相机04.jpeg | — | pending | |
| 相机05.jpeg | — | pending | |
| 自动扶梯立体剖面图生成.png | — | pending | 9/19 新增，立体剖面图 |

**遗留项**：`videos/飞机.mp4` 在 images/ 中无对应源图片，视为历史遗留，暂不处理。
