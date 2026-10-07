import { useMemo, useState } from 'react';
import { DayPicker } from 'react-day-picker';
import { zhCN } from 'date-fns/locale';
import { ChevronLeft, ChevronRight, CalendarDays, Hash } from 'lucide-react';
import {
  appLocalHourMinute,
  appLocalIso,
  computeEffectiveKickAt,
  formatAppLocalMinute,
  kickPolicyLabel,
  selectionExpiryDate,
  toAppLocal,
  type ExpirySelection,
  type KickPolicy,
} from '../lib/expiry';
import { BUTTON, INPUT } from './ui';
import { cn } from '../lib/utils';

export type { ExpirySelection } from '../lib/expiry';

type PresetTile =
  | { id: string; label: string; selection: ExpirySelection }
  | { id: 'custom'; label: string; selection: null };

/** 九宫格。默认模式的最后一格是「自定义」。 */
const TILES: PresetTile[] = [
  { id: '7d', label: '7 天', selection: { kind: 'duration', value: '7d' } },
  { id: '14d', label: '14 天', selection: { kind: 'duration', value: '14d' } },
  { id: '30d', label: '30 天', selection: { kind: 'duration', value: '30d' } },
  { id: '31d', label: '31 天', selection: { kind: 'duration', value: '31d' } },
  { id: '90d', label: '90 天', selection: { kind: 'duration', value: '90d' } },
  { id: '180d', label: '180 天', selection: { kind: 'duration', value: '180d' } },
  { id: '360d', label: '360 天', selection: { kind: 'duration', value: '360d' } },
  { id: 'never', label: '永不过期', selection: { kind: 'never' } },
  { id: 'custom', label: '自定义', selection: null },
];

const PRESET_IDS = new Set(TILES.filter((t) => t.id !== 'custom').map((t) => t.id));

interface ExpiryPickerProps {
  /** 受控用法（表单里先选后交）。不传则组件自己记住选中项。 */
  value?: ExpirySelection | null;
  /** 每次选择变化都会调用。 */
  onChange?: (selection: ExpirySelection) => void;
  /**
   * 传了就是"选完即提交"（浮层里的用法）：点九宫格立刻落地，
   * 自定义面板则由面板里的「确认」触发。
   */
  onSubmit?: (selection: ExpirySelection) => void;
  /** 成员加入时间。日历默认把时分对齐到它，与后端既有写入口径一致。 */
  joinedAt?: string | null;
  /** 来自 useSettings 的宽限规则，用来算「预计 X 移出」。 */
  policy: KickPolicy;
  disabled?: boolean;
  /**
   * 'both'     九宫格 + 自定义（时长/日期两个分页）——默认，成员行与新增对话框用。
   * 'duration' 只给时长：九宫格 + 自定义天数，没有日历。
   * 'date'     只给日期：直接展开日历 + 时分，没有九宫格。
   * 用户管理那一列坚持保留「修改日期」和「增加时长」两个按钮，各自只干一件事。
   */
  mode?: 'both' | 'duration' | 'date';
}

function clampInt(raw: string, min: number, max: number, fallback: number): number {
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(n, min), max);
}

export default function ExpiryPicker({
  value,
  onChange,
  onSubmit,
  joinedAt,
  policy,
  disabled = false,
  mode = 'both',
}: ExpiryPickerProps) {
  const dateOnly = mode === 'date';
  const durationOnly = mode === 'duration';
  const controlled = value !== undefined;
  const [internal, setInternal] = useState<ExpirySelection | null>(null);
  const current = controlled ? value ?? null : internal;

  // 单独的「增加时长」浮层始终展示输入框，避免还要先点一次「自定义」。
  const [customOpen, setCustomOpen] = useState(dateOnly || durationOnly);
  const [customMode, setCustomMode] = useState<'days' | 'date'>(dateOnly ? 'date' : 'days');
  const [amountText, setAmountText] = useState('');
  const [amountUnit, setAmountUnit] = useState<'d' | 'h' | 'm'>('d');
  const [calendarDate, setCalendarDate] = useState<Date | undefined>(undefined);

  // 日历时分的默认值 = 成员加入时间的时分（后端一直是这么写的），
  // 拿不到加入时间时（例如还不存在的新成员）退回"此刻的时分"，
  // 这样"选中 N 天后的那一天"就正好是整 N 天。
  const defaultClock = useMemo(() => {
    const joined = appLocalHourMinute(joinedAt);
    if (joined) return joined;
    const now = toAppLocal(new Date());
    return { hour: now.hour, minute: now.minute };
  }, [joinedAt]);

  const [hourText, setHourText] = useState<string>(String(defaultClock.hour).padStart(2, '0'));
  const [minuteText, setMinuteText] = useState<string>(String(defaultClock.minute).padStart(2, '0'));
  const [clockTouched, setClockTouched] = useState(false);

  // joinedAt 变了（同一个浮层换了一行成员）且管理员还没手动改过时分，跟着默认值走。
  const [prevClockKey, setPrevClockKey] = useState(`${defaultClock.hour}:${defaultClock.minute}`);
  const clockKey = `${defaultClock.hour}:${defaultClock.minute}`;
  if (prevClockKey !== clockKey) {
    setPrevClockKey(clockKey);
    if (!clockTouched) {
      setHourText(String(defaultClock.hour).padStart(2, '0'));
      setMinuteText(String(defaultClock.minute).padStart(2, '0'));
    }
  }

  const activeId = useMemo(() => {
    if (customOpen && !durationOnly) return 'custom';
    if (!current) return null;
    if (current.kind === 'never') return 'never';
    if (current.kind === 'duration' && PRESET_IDS.has(current.value)) return current.value;
    return 'custom';
  }, [current, customOpen, durationOnly]);

  /** 自定义面板当前这一刻会提交什么。 */
  const draft: ExpirySelection | null = useMemo(() => {
    if (customMode === 'days') {
      const amount = Number.parseInt(amountText, 10);
      if (!Number.isFinite(amount) || amount <= 0) return null;
      return { kind: 'duration', value: `${amount}${amountUnit}` };
    }
    if (!calendarDate) return null;
    return {
      kind: 'absolute',
      iso: appLocalIso({
        year: calendarDate.getFullYear(),
        month: calendarDate.getMonth() + 1,
        day: calendarDate.getDate(),
        hour: clampInt(hourText, 0, 23, 0),
        minute: clampInt(minuteText, 0, 59, 0),
      }),
    };
  }, [customMode, amountText, amountUnit, calendarDate, hourText, minuteText]);

  const previewSelection = durationOnly ? draft ?? current : customOpen ? draft : current;
  const previewExpiry = selectionExpiryDate(previewSelection);
  const previewKick = previewExpiry ? computeEffectiveKickAt(previewExpiry, policy) : null;

  const commit = (selection: ExpirySelection) => {
    if (!controlled) setInternal(selection);
    onChange?.(selection);
    onSubmit?.(selection);
  };

  const handleTile = (tile: PresetTile) => {
    if (tile.id === 'custom') {
      setCustomOpen((open) => !open);
      return;
    }
    if (!durationOnly) setCustomOpen(false);
    if (tile.selection) commit(tile.selection);
  };

  const handleCustomConfirm = () => {
    if (!draft) return;
    commit(draft);
    if (!durationOnly) setCustomOpen(false);
  };

  return (
    <div className="space-y-2.5">
      {!dateOnly && (
        <div className="grid grid-cols-3 gap-1.5">
          {(durationOnly ? TILES.filter((tile) => tile.id !== 'custom') : TILES).map((tile) => {
            const isActive = activeId === tile.id;
            return (
              <button
                key={tile.id}
                type="button"
                disabled={disabled}
                onClick={() => handleTile(tile)}
                aria-pressed={isActive}
                className={cn(
                  'h-9 whitespace-nowrap rounded-lg border px-2 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50',
                  isActive
                    ? 'border-blue-600 bg-blue-600 text-white shadow-sm'
                    : 'border-transparent bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-ink-800 dark:text-ink-200 dark:hover:bg-ink-700',
                )}
              >
                {tile.label}
              </button>
            );
          })}
        </div>
      )}

      {(customOpen || dateOnly) && (
        <div className={cn('space-y-2.5', !dateOnly && 'rounded-lg border border-gray-200 p-2.5 dark:border-ink-800')}>
          {mode === 'both' && (
            <div className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-ink-950">
              {([
                { id: 'days' as const, label: '按时长', Icon: Hash },
                { id: 'date' as const, label: '按日期', Icon: CalendarDays },
              ]).map(({ id, label, Icon }) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setCustomMode(id)}
                  aria-pressed={customMode === id}
                  className={cn(
                    'flex items-center justify-center gap-1.5 whitespace-nowrap rounded-md px-2 py-1.5 text-xs font-medium transition-colors',
                    customMode === id
                      ? 'bg-white text-gray-900 shadow-sm dark:bg-ink-800 dark:text-gray-100'
                      : 'text-gray-500 hover:text-gray-900 dark:text-ink-400 dark:hover:text-gray-100',
                  )}
                >
                  <Icon className="size-3.5" />
                  {label}
                </button>
              ))}
            </div>
          )}

          {customMode === 'days' ? (
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={1}
                value={amountText}
                onChange={(e) => setAmountText(e.target.value)}
                aria-label="自定义时长数值"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleCustomConfirm();
                  }
                }}
                className={cn(INPUT, 'py-1.5')}
                placeholder="数量"
              />
              <select
                value={amountUnit}
                onChange={(e) => setAmountUnit(e.target.value as 'd' | 'h' | 'm')}
                aria-label="时长单位"
                className={cn(INPUT, 'w-auto shrink-0 py-1.5')}
              >
                <option value="d">天</option>
                <option value="h">小时</option>
                <option value="m">分钟</option>
              </select>
            </div>
          ) : (
            <div className="mx-auto w-[252px] max-w-full">
              <DayPicker
                mode="single"
                locale={zhCN}
                selected={calendarDate}
                onSelect={setCalendarDate}
                disabled={{ before: new Date() }}
                defaultMonth={calendarDate || new Date()}
                classNames={{
                  root: 'relative p-0',
                  months: 'flex flex-col',
                  month: 'space-y-2',
                  month_caption: 'flex h-8 items-center justify-center',
                  caption_label: 'text-sm font-medium text-gray-900 dark:text-gray-100',
                  nav: 'absolute inset-x-0 top-0 flex items-center justify-between',
                  button_previous:
                    'flex size-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:opacity-30 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-100',
                  button_next:
                    'flex size-8 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:opacity-30 dark:text-ink-400 dark:hover:bg-ink-800 dark:hover:text-gray-100',
                  month_grid: 'w-full border-collapse',
                  weekdays: 'flex',
                  weekday: 'w-9 text-[0.75rem] font-normal text-gray-400 dark:text-ink-500',
                  week: 'mt-0.5 flex w-full',
                  day: 'size-9 p-0 text-center text-sm text-gray-700 dark:text-ink-200',
                  day_button:
                    'inline-flex size-9 items-center justify-center rounded-md p-0 font-normal transition-colors hover:bg-gray-100 dark:hover:bg-ink-800',
                  selected: '[&>button]:bg-blue-600 [&>button]:font-medium [&>button]:text-white [&>button]:hover:bg-blue-600',
                  today: '[&>button]:ring-1 [&>button]:ring-inset [&>button]:ring-gray-300 dark:[&>button]:ring-ink-600',
                  outside: '[&>button]:text-gray-300 dark:[&>button]:text-ink-600',
                  disabled: '[&>button]:cursor-not-allowed [&>button]:text-gray-300 [&>button]:hover:bg-transparent dark:[&>button]:text-ink-600',
                  hidden: 'invisible',
                }}
                components={{
                  Chevron: ({ orientation }) => {
                    const Icon = orientation === 'left' ? ChevronLeft : ChevronRight;
                    return <Icon className="size-4" />;
                  },
                }}
              />

              <div className="mt-2 flex items-center gap-2">
                <span className="text-xs text-gray-500 dark:text-ink-400">时间</span>
                <input
                  type="number"
                  min={0}
                  max={23}
                  value={hourText}
                  onChange={(e) => { setClockTouched(true); setHourText(e.target.value); }}
                  onBlur={() => setHourText(String(clampInt(hourText, 0, 23, 0)).padStart(2, '0'))}
                  aria-label="小时"
                  className={cn(INPUT, 'w-16 px-2 py-1.5 text-center')}
                />
                <span className="text-sm text-gray-400 dark:text-ink-500">:</span>
                <input
                  type="number"
                  min={0}
                  max={59}
                  value={minuteText}
                  onChange={(e) => { setClockTouched(true); setMinuteText(e.target.value); }}
                  onBlur={() => setMinuteText(String(clampInt(minuteText, 0, 59, 0)).padStart(2, '0'))}
                  aria-label="分钟"
                  className={cn(INPUT, 'w-16 px-2 py-1.5 text-center')}
                />
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={handleCustomConfirm}
            disabled={!draft || disabled}
            className={cn(BUTTON.primary, 'w-full py-1.5')}
          >
            确认
          </button>
        </div>
      )}

      <div className="min-h-4 text-xs leading-4">
        {previewSelection?.kind === 'never' ? (
          <span className="text-amber-600 dark:text-amber-400">永不过期（不自动移出）</span>
        ) : previewExpiry && previewKick ? (
          <span>
            <span className="text-blue-600 dark:text-blue-400">预计 {formatAppLocalMinute(previewKick)} 移出</span>
            <span className="text-gray-400 dark:text-ink-500">（{kickPolicyLabel(policy)}）</span>
          </span>
        ) : (
          <span className="text-gray-400 dark:text-ink-500">选择一个到期时间</span>
        )}
      </div>
    </div>
  );
}
