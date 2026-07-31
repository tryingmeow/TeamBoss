interface SeatRingProps {
  used: number;
  total: number;
  size?: number;
  label?: string;
}

export default function SeatRing({ used, total, size = 64, label }: SeatRingProps) {
  const radius = (size - 8) / 2;
  const circumference = 2 * Math.PI * radius;
  const percentage = total > 0 ? used / total : 0;
  const offset = circumference * (1 - percentage);
  const isFull = used >= total && total > 0;

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} className="-rotate-90">
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="#2a2d3a"
            strokeWidth={4}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={isFull ? '#ef4444' : '#3b82f6'}
            strokeWidth={4}
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            className="transition-all duration-500"
          />
        </svg>
        <div className="absolute inset-0 flex items-center justify-center">
          <span className={`text-sm font-bold ${isFull ? 'text-red-400' : 'text-blue-400'}`}>
            {used}/{total}
          </span>
        </div>
      </div>
      {label && <span className="text-xs text-gray-400">{label}</span>}
    </div>
  );
}
