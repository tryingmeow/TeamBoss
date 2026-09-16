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

export type { ExpirySelection } from '../lib/expiry';

type PresetTile =
  | { id: string; label: string; selection: ExpirySelection }
  | { id: 'custom'; label: string; selection: null };

/** 九宫格。顺序即视觉顺序：三行三列，最后一格永远是「自定义」。 */
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

type Tone = 'blue' | 'indigo';

/**
 * 两个调用面各自的主色：成员面板一直是 blue-600，后台管理页一直是 indigo-500。
 * 组件是同一个，只有这一张表决定它长成哪边的样子。
 */
const TONE: Record<Tone, {
  active: string;
  idle: string;
  accentText: string;
  ring: string;
  solid: string;
  calendarSelected: string;
  panelBorder: string;
  muted: string;
}> = {
  blue: {
    active: 'bg-blue-600 text-white shadow-md shadow-blue-500/20',
    idle: 'bg-gray-100 dark:bg-[#2a2d3a] text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-[#3a3d4a]',
    accentText: 'text-blue-600 dark:text-blue-400',
    ring: 'focus:ring-blue-500/50 focus:border-blue-500',
    solid: 'bg-blue-600 hover:bg-blue-700 text-white',
    calendarSelected: 'bg-blue-600 text-white hover:bg-blue-600 hover:text-white focus:bg-blue-600 focus:text-white rounded-md',
    panelBorder: 'border-gray-200 dark:border-[#2a2d3a]',
    muted: 'text-gray-500 dark:text-gray-400',
  },
  indigo: {
    active: 'bg-indigo-500 text-white shadow-md shadow-indigo-500/20',
    idle: 'bg-white dark:bg-slate-900 text-gray-700 dark:text-slate-300 border border-gray-300 dark:border-slate-700 hover:bg-indigo-500/10 hover:text-indigo-500 hover:border-indigo-500/40',
    accentText: 'text-indigo-500 dark:text-indigo-400',
    ring: 'focus:ring-indigo-500/50 focus:border-indigo-500',
    solid: 'bg-indigo-500 hover:bg-indigo-600 text-white',
    calendarSelected: 'bg-indigo-500 text-white hover:bg-indigo-500 hover:text-white focus:bg-indigo-500 focus:text-white rounded-md',
    panelBorder: 'border-gray-300 dark:border-slate-700',
    muted: 'text-gray-500 dark:text-slate-400',
  },
};

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
  tone?: Tone;
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
  tone = 'blue',
  disabled = false,
  mode = 'both',
}: ExpiryPickerProps) {
  const dateOnly = mode === 'date';
  const t = TONE[tone];
  const controlled = value !== undefined;
  const [internal, setInternal] = useState<ExpirySelection | null>(null);
  const current = controlled ? value ?? null : internal;

  const [customOpen, setCustomOpen] = useState(dateOnly);
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
    if (customOpen) return 'custom';
    if (!current) return null;
    if (current.kind === 'never') return 'never';
    if (current.kind === 'duration' && PRESET_IDS.has(current.value)) return current.value;
    return 'custom';
  }, [current, customOpen]);

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

  const previewSelection = customOpen ? draft : current;
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
    setCustomOpen(false);
    if (tile.selection) commit(tile.selection);
  };

  const handleCustomConfirm = () => {
    if (!draft) return;
    commit(draft);
    setCustomOpen(false);
  };

  return (
    <div className="space-y-2.5">
      {!dateOnly && (
      <div className="grid grid-cols-3 gap-1.5">
        {TILES.map((tile) => {
          const isActive = activeId === tile.id;
          return (
            <button
              key={tile.id}
              type="button"
              disabled={disabled}
              onClick={() => handleTile(tile)}
              aria-pressed={isActive}
              className={`rounded-lg px-2 py-2 text-xs font-medium transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
                isActive ? t.active : t.idle
              }`}
            >
              {tile.label}
            </button>
          );
        })}
      </div>
      )}

      {(customOpen || dateOnly) && (
        <div className={`rounded-lg border p-2.5 space-y-2.5 ${t.panelBorder}`}>
          {mode === 'both' && (
          <div className="flex gap-1">
            {([
              { id: 'days' as const, label: '按时长', Icon: Hash },
              { id: 'date' as const, label: '按日期', Icon: CalendarDays },
            ]).map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => setCustomMode(id)}
                className={`flex flex-1 items-center justify-center gap-1 rounded-md px-2 py-1.5 text-xs font-medium transition-colors ${
                  customMode === id
                    ? t.active
                    : `${t.muted} hover:bg-gray-100 dark:hover:bg-white/5`
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
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
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleCustomConfirm();
                  }
                }}
                className={`w-full rounded-lg border border-gray-200 bg-gray-50 px-3 py-1.5 text-sm text-gray-900 transition-all focus:outline-none focus:ring-2 dark:border-[#2a2d3a] dark:bg-[#0f1117] dark:text-gray-200 ${t.ring}`}
                placeholder="天数"
              />
              <select
                value={amountUnit}
                onChange={(e) => setAmountUnit(e.target.value as 'd' | 'h' | 'm')}
                className={`shrink-0 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1.5 text-sm text-gray-900 focus:outline-none focus:ring-2 dark:border-[#2a2d3a] dark:bg-[#0f1117] dark:text-gray-200 ${t.ring}`}
              >
                <option value="d">天</option>
                <option value="h">小时</option>
                <option value="m">分钟</option>
              </select>
            </div>
          ) : (
            <div className="w-[264px] max-w-full">
              <DayPicker
                mode="single"
                locale={zhCN}
                selected={calendarDate}
                onSelect={setCalendarDate}
                disabled={{ before: new Date() }}
                defaultMonth={calendarDate || new Date()}
                classNames={{
                  root: 'p-0',
                  months: 'flex flex-col space-y-4',
                  month: 'space-y-2',
                  month_caption: 'flex justify-center pt-1 relative items-center',
                  caption_label: 'text-sm font-medium text-gray-800 dark:text-gray-200',
                  nav: 'space-x-1 flex items-center',
                  button_previous:
                    'absolute left-1 h-7 w-7 bg-transparent p-0 opacity-50 hover:opacity-100 flex items-center justify-center text-gray-500 dark:text-gray-400',
                  button_next:
                    'absolute right-1 h-7 w-7 bg-transparent p-0 opacity-50 hover:opacity-100 flex items-center justify-center text-gray-500 dark:text-gray-400',
                  month_grid: 'w-full border-collapse',
                  weekdays: 'flex',
                  weekday: 'text-gray-400 dark:text-gray-500 rounded-md w-8 font-normal text-[0.75rem]',
                  week: 'flex w-full mt-1',
                  day: 'h-8 w-8 text-center text-sm p-0 relative',
                  day_button:
                    'h-8 w-8 p-0 font-normal rounded-md transition-colors inline-flex items-center justify-center text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-[#2a2d3a]',
                  selected: t.calendarSelected,
                  today: 'bg-gray-100 dark:bg-[#2a2d3a] rounded-md',
                  outside: 'text-gray-400 dark:text-gray-600 opacity-50',
                  disabled: 'text-gray-400 dark:text-gray-600 opacity-50',
                  hidden: 'invisible',
                }}
                components={{
                  Chevron: ({ orientation }) => {
                    const Icon = orientation === 'left' ? ChevronLeft : ChevronRight;
                    return <Icon className="h-4 w-4" />;
                  },
                }}
              />

              <div className="mt-2 flex items-center gap-2">
                <span className={`text-xs ${t.muted}`}>时间</span>
                <input
                  type="number"
                  min={0}
                  max={23}
                  value={hourText}
                  onChange={(e) => { setClockTouched(true); setHourText(e.target.value); }}
                  onBlur={() => setHourText(String(clampInt(hourText, 0, 23, 0)).padStart(2, '0'))}
                  aria-label="小时"
                  className={`w-14 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-center text-sm text-gray-900 focus:outline-none focus:ring-2 dark:border-[#2a2d3a] dark:bg-[#0f1117] dark:text-gray-200 ${t.ring}`}
                />
                <span className="text-sm text-gray-400">:</span>
                <input
                  type="number"
                  min={0}
                  max={59}
                  value={minuteText}
                  onChange={(e) => { setClockTouched(true); setMinuteText(e.target.value); }}
                  onBlur={() => setMinuteText(String(clampInt(minuteText, 0, 59, 0)).padStart(2, '0'))}
                  aria-label="分钟"
                  className={`w-14 rounded-lg border border-gray-200 bg-gray-50 px-2 py-1 text-center text-sm text-gray-900 focus:outline-none focus:ring-2 dark:border-[#2a2d3a] dark:bg-[#0f1117] dark:text-gray-200 ${t.ring}`}
                />
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={handleCustomConfirm}
            disabled={!draft || disabled}
            className={`w-full rounded-lg px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${t.solid}`}
          >
            确认
          </button>
        </div>
      )}

      <div className="min-h-[1rem] text-[11px] leading-4">
        {previewSelection?.kind === 'never' ? (
          <span className="text-amber-600 dark:text-amber-400">永不过期</span>
        ) : previewExpiry && previewKick ? (
          <span className={t.muted}>
            <span className={t.accentText}>预计 {formatAppLocalMinute(previewKick)} 移出</span>
            <span className="text-gray-400 dark:text-gray-500">（{kickPolicyLabel(policy)}）</span>
          </span>
        ) : (
          <span className="text-gray-400 dark:text-gray-600">选择一个到期时间</span>
        )}
      </div>
    </div>
  );
}
