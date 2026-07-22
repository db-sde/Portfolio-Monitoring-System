export default function SkeletonTable({ rows = 5, cols = 6 }) {
  return (
    <div className="animate-pulse rounded-xl border border-line-soft bg-card overflow-hidden">
      <div className="h-10 bg-paper-soft border-b border-line-soft" />
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-4 px-4 py-3.5 border-b border-line-soft last:border-0">
          {Array.from({ length: cols }).map((_, c) => (
            <div key={c} className="h-4 bg-paper-soft rounded flex-1" />
          ))}
        </div>
      ))}
    </div>
  )
}
