export default function SkeletonTable({ rows = 5, cols = 6 }) {
  return (
    <div className="animate-pulse rounded-lg border border-gray-200 bg-white overflow-hidden">
      <div className="h-9 bg-gray-100 border-b border-gray-200" />
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex gap-4 px-4 py-3 border-b border-gray-100 last:border-0">
          {Array.from({ length: cols }).map((_, c) => (
            <div key={c} className="h-4 bg-gray-100 rounded flex-1" />
          ))}
        </div>
      ))}
    </div>
  )
}
