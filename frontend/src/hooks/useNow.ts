import { useEffect, useState } from 'react';

/** 当前时间戳，每 intervalMs 更新一次，让「N 分钟前」这类文字在页面开着时保持准确。 */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}
