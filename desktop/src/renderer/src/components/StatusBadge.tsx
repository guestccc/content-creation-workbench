/** 状态徽标：把状态码渲染成带颜色的小标签。 */

interface Props {
  label: string
  color: string
}

export default function StatusBadge({ label, color }: Props): JSX.Element {
  return (
    <span
      className="badge badge-dot"
      style={{ color, backgroundColor: `${color}1a` }}
    >
      {label}
    </span>
  )
}
