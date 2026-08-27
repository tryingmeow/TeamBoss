import {
  ArrowUp,
  CalendarClock,
  Check,
  ChevronDown,
  Gauge,
  Type as TypeIcon,
} from 'lucide-react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

export type SortKey = 'name' | 'renewal' | 'idle';
export type SortDirection = 'asc' | 'desc';

interface DashboardSortControlProps {
  sortKey: SortKey;
  sortDirection: SortDirection;
  onSortKeyChange: (value: SortKey) => void;
  onSortDirectionChange: (value: SortDirection) => void;
}

const SORT_LABELS: Record<SortKey, string> = {
  name: '名称',
  renewal: '距续费时间',
  idle: 'GPT 空闲率',
};

const SORT_OPTIONS = [
  { value: 'name', label: SORT_LABELS.name, icon: TypeIcon },
  { value: 'renewal', label: SORT_LABELS.renewal, icon: CalendarClock },
  { value: 'idle', label: SORT_LABELS.idle, icon: Gauge },
] satisfies Array<{ value: SortKey; label: string; icon: typeof TypeIcon }>;

function getDirectionLabel(sortKey: SortKey, sortDirection: SortDirection): string {
  if (sortKey === 'name') return sortDirection === 'asc' ? 'A → Z' : 'Z → A';
  if (sortKey === 'renewal') return sortDirection === 'asc' ? '近 → 远' : '远 → 近';
  return sortDirection === 'asc' ? '低 → 高' : '高 → 低';
}

export default function DashboardSortControl({
  sortKey,
  sortDirection,
  onSortKeyChange,
  onSortDirectionChange,
}: DashboardSortControlProps) {
  const directionLabel = getDirectionLabel(sortKey, sortDirection);
  const nextDirection = sortDirection === 'asc' ? 'desc' : 'asc';
  const nextDirectionLabel = getDirectionLabel(sortKey, nextDirection);
  const activeSortOption = SORT_OPTIONS.find((option) => option.value === sortKey) ?? SORT_OPTIONS[2];
  const ActiveSortIcon = activeSortOption.icon;

  return (
    <div
      className="inline-flex h-11 items-stretch overflow-hidden rounded-[14px] border border-gray-200/90 bg-white/90 shadow-[0_1px_2px_rgba(15,23,42,0.04),0_8px_24px_rgba(15,23,42,0.04)] backdrop-blur-xl transition-all duration-200 hover:border-gray-300 hover:shadow-[0_2px_4px_rgba(15,23,42,0.05),0_10px_28px_rgba(15,23,42,0.07)] focus-within:border-blue-400 focus-within:ring-4 focus-within:ring-blue-500/10 dark:border-white/[0.08] dark:bg-[#1a1d27]/90 dark:shadow-[0_8px_24px_rgba(0,0,0,0.18)] dark:hover:border-white/[0.14]"
      role="group"
      aria-label="Team 排序"
    >
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            className="group inline-flex min-w-0 items-center gap-2.5 px-2.5 text-left outline-none transition-colors duration-200 hover:bg-gray-50/90 data-[state=open]:bg-gray-50 sm:min-w-[10.75rem] dark:hover:bg-white/[0.04] dark:data-[state=open]:bg-white/[0.05]"
            aria-label={`排序方式：${activeSortOption.label}，当前 ${directionLabel}`}
          >
            <span className="grid size-7 shrink-0 place-items-center rounded-[9px] bg-blue-50 text-blue-600 ring-1 ring-blue-100/80 transition-colors duration-200 group-hover:bg-blue-100/80 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-400/10">
              <ActiveSortIcon size={14} strokeWidth={2} />
            </span>
            <span className="min-w-0 flex-1 leading-none">
              <span className="block truncate text-[13px] font-semibold text-gray-800 dark:text-gray-100">
                {activeSortOption.label}
              </span>
              <span className="mt-1 block text-[10px] font-medium tracking-wide text-gray-400 dark:text-gray-500">
                {directionLabel}
              </span>
            </span>
            <ChevronDown
              size={14}
              strokeWidth={2}
              className="shrink-0 text-gray-400 transition-transform duration-200 group-data-[state=open]:rotate-180 dark:text-gray-500"
            />
          </button>
        </DropdownMenu.Trigger>

        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={8}
            collisionPadding={12}
            className="sort-menu-content z-50 min-w-[13.5rem] rounded-2xl border border-gray-200/90 bg-white/95 p-1.5 shadow-[0_18px_50px_rgba(15,23,42,0.14)] backdrop-blur-xl outline-none dark:border-white/[0.09] dark:bg-[#1a1d27]/95 dark:shadow-[0_20px_55px_rgba(0,0,0,0.38)]"
          >
            <DropdownMenu.RadioGroup
              value={sortKey}
              onValueChange={(value) => onSortKeyChange(value as SortKey)}
            >
              {SORT_OPTIONS.map((option) => {
                const OptionIcon = option.icon;
                return (
                  <DropdownMenu.RadioItem
                    key={option.value}
                    value={option.value}
                    className="group relative flex cursor-default select-none items-center gap-3 rounded-xl px-2.5 py-2.5 pr-9 text-[13px] font-medium text-gray-700 outline-none transition-colors duration-150 data-[highlighted]:bg-gray-100 data-[state=checked]:bg-blue-50 data-[state=checked]:text-blue-700 dark:text-gray-300 dark:data-[highlighted]:bg-white/[0.06] dark:data-[state=checked]:bg-blue-500/10 dark:data-[state=checked]:text-blue-300"
                  >
                    <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-gray-100 text-gray-500 transition-colors group-data-[state=checked]:bg-white group-data-[state=checked]:text-blue-600 dark:bg-white/[0.05] dark:text-gray-400 dark:group-data-[state=checked]:bg-blue-500/10 dark:group-data-[state=checked]:text-blue-400">
                      <OptionIcon size={14} strokeWidth={2} />
                    </span>
                    <span>{option.label}</span>
                    <DropdownMenu.ItemIndicator className="absolute right-3 grid place-items-center text-blue-600 dark:text-blue-400">
                      <Check size={15} strokeWidth={2.4} />
                    </DropdownMenu.ItemIndicator>
                  </DropdownMenu.RadioItem>
                );
              })}
            </DropdownMenu.RadioGroup>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <span className="my-2 w-px shrink-0 bg-gray-200/80 dark:bg-white/[0.08]" aria-hidden="true" />

      <button
        type="button"
        onClick={() => onSortDirectionChange(nextDirection)}
        aria-label={`切换排序方向，当前 ${directionLabel}，点击切换为 ${nextDirectionLabel}`}
        title={`切换为 ${nextDirectionLabel}`}
        className="group grid w-10 shrink-0 place-items-center text-gray-500 outline-none transition-colors duration-200 hover:bg-blue-50 hover:text-blue-600 focus-visible:bg-blue-50 focus-visible:text-blue-600 dark:text-gray-400 dark:hover:bg-blue-500/10 dark:hover:text-blue-400 dark:focus-visible:bg-blue-500/10 dark:focus-visible:text-blue-400"
      >
        <ArrowUp
          size={16}
          strokeWidth={2.2}
          className={`transition-transform duration-300 ease-out ${sortDirection === 'desc' ? 'rotate-180' : 'rotate-0'}`}
        />
      </button>
    </div>
  );
}
