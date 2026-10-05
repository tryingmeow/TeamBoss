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
      className="inline-flex h-11 shrink-0 items-stretch overflow-hidden rounded-lg border border-gray-200 bg-white transition-colors hover:border-gray-300 has-[:focus-visible]:border-blue-500 has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-blue-500/30 dark:border-ink-800 dark:bg-ink-900 dark:hover:border-ink-700"
      role="group"
      aria-label="Team 排序"
    >
      <DropdownMenu.Root>
        <DropdownMenu.Trigger asChild>
          <button
            type="button"
            className="group inline-flex w-11 items-center justify-center gap-2.5 text-left outline-none transition-colors hover:bg-gray-50 data-[state=open]:bg-gray-50 sm:w-auto sm:min-w-[10.75rem] sm:justify-start sm:px-2.5 dark:hover:bg-ink-800/60 dark:data-[state=open]:bg-ink-800/60"
            aria-label={`排序方式：${activeSortOption.label}，当前 ${directionLabel}`}
            title={`排序：${activeSortOption.label}（${directionLabel}）`}
          >
            <span className="grid size-7 shrink-0 place-items-center rounded-md bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-400">
              <ActiveSortIcon size={14} strokeWidth={2} />
            </span>
            <span className="hidden min-w-0 flex-1 leading-none sm:block">
              <span className="block truncate text-[13px] font-semibold text-gray-800 dark:text-gray-100">
                {activeSortOption.label}
              </span>
              <span className="mt-1 block whitespace-nowrap text-[11px] text-gray-500 dark:text-ink-400">
                {directionLabel}
              </span>
            </span>
            <ChevronDown
              size={14}
              strokeWidth={2}
              className="hidden shrink-0 text-gray-400 transition-transform duration-200 group-data-[state=open]:rotate-180 sm:block dark:text-ink-500"
            />
          </button>
        </DropdownMenu.Trigger>

        <DropdownMenu.Portal>
          <DropdownMenu.Content
            align="end"
            sideOffset={8}
            collisionPadding={12}
            className="sort-menu-content z-50 min-w-[13rem] rounded-xl border border-gray-200 bg-white p-1.5 shadow-xl outline-none dark:border-ink-800 dark:bg-ink-900"
          >
            <DropdownMenu.Label className="px-2.5 pb-1 pt-1.5 text-xs font-medium text-gray-400 dark:text-ink-500">
              排序方式
            </DropdownMenu.Label>
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
                    className="group flex cursor-default select-none items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm text-gray-700 outline-none data-[highlighted]:bg-gray-100 data-[highlighted]:text-gray-900 data-[state=checked]:font-medium data-[state=checked]:text-blue-700 dark:text-gray-200 dark:data-[highlighted]:bg-ink-800 dark:data-[highlighted]:text-gray-50 dark:data-[state=checked]:text-blue-300"
                  >
                    <OptionIcon
                      size={16}
                      className="shrink-0 text-gray-400 group-data-[state=checked]:text-blue-600 dark:text-ink-400 dark:group-data-[state=checked]:text-blue-400"
                    />
                    <span className="flex-1">{option.label}</span>
                    <DropdownMenu.ItemIndicator>
                      <Check size={15} className="text-blue-600 dark:text-blue-400" />
                    </DropdownMenu.ItemIndicator>
                  </DropdownMenu.RadioItem>
                );
              })}
            </DropdownMenu.RadioGroup>
          </DropdownMenu.Content>
        </DropdownMenu.Portal>
      </DropdownMenu.Root>

      <span className="my-2 w-px shrink-0 bg-gray-200 dark:bg-ink-800" aria-hidden="true" />

      <button
        type="button"
        onClick={() => onSortDirectionChange(nextDirection)}
        aria-label={`切换排序方向，当前 ${directionLabel}，点击切换为 ${nextDirectionLabel}`}
        title={`切换为 ${nextDirectionLabel}`}
        className="grid w-10 shrink-0 place-items-center text-gray-500 outline-none transition-colors hover:bg-gray-50 hover:text-blue-600 focus-visible:bg-blue-50 focus-visible:text-blue-600 dark:text-ink-400 dark:hover:bg-ink-800/60 dark:hover:text-blue-400 dark:focus-visible:bg-blue-500/10 dark:focus-visible:text-blue-400"
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
