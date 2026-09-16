import { useEffect, useMemo, useRef, useState } from 'react';
import { BarChart3, LineChart, Table2 } from 'lucide-react';
import { getFinanceTrends, type FinanceDailyTotal } from '../api/client';

/**
 * 支出趋势：billing_snapshots 每天落一条，这里把 `daily_total_base` 画成一条线。
 *
 * 只有一个系列，所以不需要图例（标题就是系列名）；也没有引入任何图表库——
 * 一条折线不值得为它背一个依赖。
 */

/**
 * 单系列颜色。同一个值在浅色和深色底上都通过了调色板校验
 * （亮度带 / 彩度下限 / 对比度），所以两套主题共用一个 hue，不做自动翻转。
 */
const SERIES = '#6366f1';

const RANGES = [30, 90, 180, 365] as const;
type Range = (typeof RANGES)[number];

const PLOT_HEIGHT = 180;
const AXIS_BAND = 26;
const PAD_LEFT = 52;
const PAD_RIGHT = 16;
const PAD_TOP = 12;

interface Point {
  date: string;
  value: number;
  ts: number;
}

const DAY_MS = 86_400_000;

function parseDay(date: string): number {
  const ts = Date.parse(`${date}T00:00:00Z`);
  return Number.isNaN(ts) ? NaN : ts;
}

function toPoints(rows: FinanceDailyTotal[]): Point[] {
  return rows
    .map((row) => ({ date: row.date, value: Number(row.total_base), ts: parseDay(row.date) }))
    .filter((p) => Number.isFinite(p.value) && Number.isFinite(p.ts))
    .sort((a, b) => a.ts - b.ts);
}

/**
 * 把点切成若干连续段：快照缺了几天就断开，而不是用一条直线把缺口连起来
 * 假装那几天有数据。阈值取"典型采样间隔的 1.5 倍"，至少 2 天。
 */
function splitSegments(points: Point[]): Point[][] {
  if (points.length < 2) return points.length ? [points] : [];
  const gaps = points.slice(1).map((p, i) => p.ts - points[i].ts).sort((a, b) => a - b);
  const median = gaps[Math.floor(gaps.length / 2)] || DAY_MS;
  const limit = Math.max(2 * DAY_MS, median * 1.5);

  const segments: Point[][] = [];
  let current: Point[] = [points[0]];
  for (let i = 1; i < points.length; i++) {
    if (points[i].ts - points[i - 1].ts > limit) {
      segments.push(current);
      current = [points[i]];
    } else {
      current.push(points[i]);
    }
  }
  segments.push(current);
  return segments;
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag * 10;
  const ticks: number[] = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

function formatTick(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 10_000) return `${(v / 1000).toFixed(abs >= 100_000 ? 0 : 1)}k`;
  if (abs >= 100) return v.toFixed(0);
  return v.toFixed(abs >= 10 ? 1 : 2);
}

function formatMoney(v: number): string {
  return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDay(date: string): string {
  return date.length >= 10 ? date.slice(5) : date;
}

export default function CostTrendChart() {
  const [range, setRange] = useState<Range>(90);
  const [rows, setRows] = useState<FinanceDailyTotal[] | null>(null);
  const [currency, setCurrency] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [view, setView] = useState<'chart' | 'table'>('chart');
  const [hover, setHover] = useState<number | null>(null);

  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [width, setWidth] = useState(720);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setWidth(Math.max(280, el.clientWidth));
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
    // loading 也要进依赖：加载中时 wrapRef 指向的节点还没挂上，
    // 只依赖 view 的话测量永远停在初始宽度，图表在任何视口都只占半张卡。
  }, [view, loading]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getFinanceTrends(range)
      .then((res) => {
        if (cancelled) return;
        setRows(res.daily_total_base || []);
        setCurrency(res.base_currency || '');
        setError('');
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : '加载趋势数据失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range]);

  const points = useMemo(() => toPoints(rows ?? []), [rows]);

  const geometry = useMemo(() => {
    if (points.length === 0) return null;
    const innerW = Math.max(1, width - PAD_LEFT - PAD_RIGHT);
    const minTs = points[0].ts;
    const maxTs = points[points.length - 1].ts;
    const spanTs = maxTs - minTs;

    const values = points.map((p) => p.value);
    const rawMin = Math.min(...values);
    const rawMax = Math.max(...values);
    // 单点、或一条完全水平的线：给一个对称的窗口，别让它贴着边框。
    const pad = rawMax === rawMin ? Math.max(Math.abs(rawMax) * 0.1, 1) : (rawMax - rawMin) * 0.15;
    const yMin = rawMin - pad;
    const yMax = rawMax + pad;

    const x = (ts: number) => PAD_LEFT + (spanTs === 0 ? innerW / 2 : ((ts - minTs) / spanTs) * innerW);
    const y = (v: number) => PAD_TOP + (1 - (v - yMin) / (yMax - yMin)) * (PLOT_HEIGHT - PAD_TOP);

    return { x, y, yMin, yMax, innerW, minTs, maxTs, spanTs };
  }, [points, width]);

  const latest = points.length ? points[points.length - 1] : null;
  const first = points.length ? points[0] : null;
  const delta = latest && first ? latest.value - first.value : 0;

  const hoverPoint = hover !== null ? points[hover] ?? null : null;

  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!geometry || points.length === 0) return;
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < points.length; i++) {
      const d = Math.abs(geometry.x(points[i].ts) - px);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    }
    setHover(best);
  };

  const ticks = geometry ? niceTicks(geometry.yMin, geometry.yMax) : [];
  const segments = useMemo(() => splitSegments(points), [points]);

  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:shadow-none sm:p-6">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-slate-100">
            <BarChart3 className="h-4 w-4 text-indigo-500 dark:text-indigo-400" />
            支出趋势
          </div>
          <div className="mt-1 text-xs text-gray-500 dark:text-slate-400">
            月预计支出趋势{currency ? `（${currency}）` : ''}
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="inline-flex rounded-lg border border-gray-200 bg-gray-100/80 p-0.5 dark:border-slate-800 dark:bg-slate-950/60">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => { setRange(r); setHover(null); }}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  range === r
                    ? 'bg-white text-indigo-600 shadow-sm dark:bg-slate-800 dark:text-indigo-300'
                    : 'text-gray-500 hover:text-gray-900 dark:text-slate-400 dark:hover:text-slate-200'
                }`}
              >
                {r}天
              </button>
            ))}
          </div>
          <button
            type="button"
            onClick={() => setView((v) => (v === 'chart' ? 'table' : 'chart'))}
            title={view === 'chart' ? '切换到表格' : '切换到图表'}
            aria-label={view === 'chart' ? '切换到表格' : '切换到图表'}
            className="rounded-lg border border-gray-200 p-1.5 text-gray-500 transition-colors hover:text-indigo-500 dark:border-slate-800 dark:text-slate-400 dark:hover:text-indigo-400"
          >
            {view === 'chart' ? <Table2 className="h-4 w-4" /> : <LineChart className="h-4 w-4" />}
          </button>
        </div>
      </div>

      {loading && rows === null ? (
        <div className="h-[206px] animate-pulse rounded-lg bg-gray-100 dark:bg-slate-800" />
      ) : error ? (
        <div className="py-10 text-center text-sm text-rose-600 dark:text-rose-400">{error}</div>
      ) : points.length === 0 ? (
        <div className="py-10 text-center text-sm text-gray-500 dark:text-slate-400">
          暂无账单数据
        </div>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="text-2xl font-bold text-gray-900 dark:text-slate-100">
              {formatMoney(latest!.value)}
            </span>
            <span className="text-xs text-gray-500 dark:text-slate-400">
              {currency} · {formatDay(latest!.date)}
            </span>
            {points.length > 1 && (
              <span
                className={`text-xs font-medium ${
                  delta > 0
                    ? 'text-rose-600 dark:text-rose-400'
                    : delta < 0
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : 'text-gray-500 dark:text-slate-400'
                }`}
              >
                {delta > 0 ? '+' : ''}
                {formatMoney(delta)} 较 {formatDay(first!.date)}
              </span>
            )}
            <span className="text-xs text-gray-400 dark:text-slate-500">
              统计天数：{points.length} 天
            </span>
          </div>

          {view === 'table' ? (
            <div className="max-h-[206px] overflow-y-auto rounded-lg border border-gray-100 dark:border-slate-800">
              <table className="w-full text-left text-xs">
                <thead className="sticky top-0 bg-gray-50 text-gray-500 dark:bg-slate-950/60 dark:text-slate-400">
                  <tr>
                    <th className="px-3 py-2 font-medium">日期</th>
                    <th className="px-3 py-2 text-right font-medium">合计 {currency}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-slate-800">
                  {[...points].reverse().map((p) => (
                    <tr key={p.date}>
                      <td className="px-3 py-1.5 text-gray-600 dark:text-slate-300">{p.date}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-gray-900 dark:text-slate-100">
                        {formatMoney(p.value)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div ref={wrapRef} className="relative">
              <svg
                width={width}
                height={PLOT_HEIGHT + AXIS_BAND}
                role="img"
                aria-label={`支出趋势，${points.length} 个快照日，最新 ${formatMoney(latest!.value)} ${currency}`}
                onMouseMove={handleMove}
                onMouseLeave={() => setHover(null)}
                className="block touch-none"
              >
                {/* 网格与坐标轴：实线发丝线，比表面暗一档，不抢数据的视线 */}
                {geometry && ticks.map((t) => (
                  <g key={t}>
                    <line
                      x1={PAD_LEFT}
                      x2={width - PAD_RIGHT}
                      y1={geometry.y(t)}
                      y2={geometry.y(t)}
                      className="stroke-gray-200 dark:stroke-slate-800"
                      strokeWidth={1}
                    />
                    <text
                      x={PAD_LEFT - 8}
                      y={geometry.y(t)}
                      textAnchor="end"
                      dominantBaseline="middle"
                      className="fill-gray-400 text-[10px] tabular-nums dark:fill-slate-500"
                    >
                      {formatTick(t)}
                    </text>
                  </g>
                ))}

                {geometry && segments.map((seg, i) => {
                  if (seg.length === 1) {
                    return (
                      <circle
                        key={`seg-${i}`}
                        cx={geometry.x(seg[0].ts)}
                        cy={geometry.y(seg[0].value)}
                        r={4}
                        fill={SERIES}
                      />
                    );
                  }
                  const d = seg
                    .map((p, j) => `${j === 0 ? 'M' : 'L'}${geometry.x(p.ts).toFixed(2)},${geometry.y(p.value).toFixed(2)}`)
                    .join(' ');
                  return (
                    <path
                      key={`seg-${i}`}
                      d={d}
                      fill="none"
                      stroke={SERIES}
                      strokeWidth={2}
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  );
                })}

                {/* 点少的时候把每个快照日都标出来，让"只有三天数据"一眼可见 */}
                {geometry && points.length <= 30 && points.map((p) => (
                  <circle
                    key={p.date}
                    cx={geometry.x(p.ts)}
                    cy={geometry.y(p.value)}
                    r={3}
                    fill={SERIES}
                    className="stroke-white dark:stroke-slate-900"
                    strokeWidth={2}
                  />
                ))}

                {geometry && hoverPoint && (
                  <>
                    <line
                      x1={geometry.x(hoverPoint.ts)}
                      x2={geometry.x(hoverPoint.ts)}
                      y1={PAD_TOP}
                      y2={PLOT_HEIGHT}
                      className="stroke-gray-300 dark:stroke-slate-600"
                      strokeWidth={1}
                    />
                    <circle
                      cx={geometry.x(hoverPoint.ts)}
                      cy={geometry.y(hoverPoint.value)}
                      r={5}
                      fill={SERIES}
                      className="stroke-white dark:stroke-slate-900"
                      strokeWidth={2}
                    />
                  </>
                )}

                {/* x 轴：只标首尾（以及悬停点），密集刻度对一条趋势线没有价值 */}
                {geometry && (
                  <>
                    <text
                      x={PAD_LEFT}
                      y={PLOT_HEIGHT + 18}
                      textAnchor="start"
                      className="fill-gray-400 text-[10px] tabular-nums dark:fill-slate-500"
                    >
                      {formatDay(points[0].date)}
                    </text>
                    {points.length > 1 && (
                      <text
                        x={width - PAD_RIGHT}
                        y={PLOT_HEIGHT + 18}
                        textAnchor="end"
                        className="fill-gray-400 text-[10px] tabular-nums dark:fill-slate-500"
                      >
                        {formatDay(points[points.length - 1].date)}
                      </text>
                    )}
                  </>
                )}
              </svg>

              {geometry && hoverPoint && (
                <div
                  className="pointer-events-none absolute z-10 -translate-x-1/2 rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-xs shadow-lg dark:border-slate-700 dark:bg-slate-800"
                  style={{
                    left: Math.min(Math.max(geometry.x(hoverPoint.ts), 60), width - 60),
                    top: Math.max(0, geometry.y(hoverPoint.value) - 52),
                  }}
                >
                  <div className="text-gray-500 dark:text-slate-400">{hoverPoint.date}</div>
                  <div className="font-medium tabular-nums text-gray-900 dark:text-slate-100">
                    {formatMoney(hoverPoint.value)} {currency}
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
