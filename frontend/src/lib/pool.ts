/**
 * Runs `worker` over `items` with at most `limit` calls in flight. Results come back in input
 * order as settled values, so one failed item never rejects the whole batch.
 * `onProgress(done, total)` fires after each item finishes, success or failure.
 */
export async function runPool<T, R>(
  items: readonly T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>,
  onProgress?: (done: number, total: number) => void,
): Promise<PromiseSettledResult<R>[]> {
  const total = items.length;
  const results = new Array<PromiseSettledResult<R>>(total);
  let next = 0;
  let done = 0;

  const lane = async () => {
    while (next < total) {
      const index = next++;
      try {
        results[index] = { status: 'fulfilled', value: await worker(items[index], index) };
      } catch (reason) {
        results[index] = { status: 'rejected', reason };
      }
      done += 1;
      onProgress?.(done, total);
    }
  };

  const lanes = Math.max(1, Math.min(Math.floor(limit) || 1, total));
  await Promise.all(Array.from({ length: lanes }, lane));
  return results;
}

/** Readable message from a rejected pool item (or any caught value). */
export function errorText(reason: unknown, fallback = '未知错误'): string {
  if (reason instanceof Error && reason.message) return reason.message;
  if (typeof reason === 'string' && reason) return reason;
  return fallback;
}
