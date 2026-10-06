import { useState, type ReactElement, type ReactNode } from 'react';
import { Check } from 'lucide-react';
import { ApiError, OverageConfirmationError, OverageForbiddenError, type OverageConfirmation } from '../api/client';
import { newOverageConfirmation } from '../lib/overageConfirmation';
import { SEAT_STYLE, SEAT_TYPE_OPTIONS, SEAT_TYPES, seatUpdateErrorMessage } from '../lib/seatType';
import { gateMessage, gateShortHint, switchConfirmText, type SeatGate } from '../lib/seatCapacity';
import type { SeatType, ShowToast } from '../types';
import ConfirmDialog from './ConfirmDialog';
import SeatBetaBadge from './BetaBadge';
import { cn } from '../lib/utils';

interface SeatSwitchOptionsProps {
  current: SeatType;
  /** Cached gate per target; null = nothing cached, the server decides. */
  gateFor: (seatType: SeatType) => SeatGate | null;
  disabled?: boolean;
  onPick: (seatType: SeatType, gate: SeatGate | null) => void;
  /** Lets a caller wrap each row (e.g. in Popover.Close). */
  wrap?: (row: ReactElement, seatType: SeatType) => ReactNode;
}

/**
 * The rows of a seat-type menu. A billed target with no free seat shows what the Team's
 * overage policy will do; under 禁止超员 the row is disabled.
 */
export function SeatSwitchOptions({ current, gateFor, disabled, onPick, wrap }: SeatSwitchOptionsProps) {
  return (
    <>
      {SEAT_TYPE_OPTIONS.map(({ value, label }) => {
        const isCurrent = value === current;
        const gate = isCurrent ? null : gateFor(value);
        const hint = gateShortHint(gate);
        const blocked = gate?.action === 'forbid';
        const row = (
          <button
            key={value}
            type="button"
            onClick={() => {
              if (!isCurrent) onPick(value, gate);
            }}
            disabled={disabled || blocked}
            aria-disabled={isCurrent || undefined}
            title={gate ? gateMessage(gate) ?? undefined : undefined}
            className={cn(
              'flex min-h-9 w-full items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-gray-700 transition-colors hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-ink-800',
              blocked ? 'cursor-not-allowed opacity-55 hover:bg-transparent dark:hover:bg-transparent' : 'disabled:cursor-wait disabled:opacity-60',
            )}
          >
            <span className="flex min-w-0 items-start gap-2">
              <span className={cn('mt-1.5 size-2 shrink-0 rounded-full', SEAT_STYLE[value].solid)} aria-hidden />
              <span className="min-w-0">
                <span className="flex items-center gap-1.5">
                  {label}
                  <SeatBetaBadge seatType={value} />
                </span>
                {hint && (
                  <span
                    className={cn(
                      'block text-[11px] leading-4',
                      blocked ? 'text-gray-500 dark:text-ink-400' : 'text-amber-700 dark:text-amber-400',
                    )}
                  >
                    {hint}
                  </span>
                )}
              </span>
            </span>
            {isCurrent && <Check size={14} className="shrink-0 text-blue-600 dark:text-blue-400" />}
          </button>
        );
        return wrap ? wrap(row, value) : row;
      })}
    </>
  );
}

interface UseSeatSwitchOptions {
  /**
   * Performs the switch. `confirmation` is set only after the admin confirmed the charge: one
   * fresh confirmation for one seat of that type.
   */
  apply: (seatType: SeatType, confirmation: OverageConfirmation | null) => Promise<unknown>;
  onSwitched: () => void;
  showToast: ShowToast;
  isCodexEnabled?: boolean | number;
  /** Called when a confirm dialog is about to open (close the menu under it). */
  onAsk?: () => void;
}

/**
 * Switch flow shared by every seat menu: free target → switch; full + 超员需确认 → ask first,
 * saying a seat will be bought; the server's 409s (it re-checks live) are handled the same way.
 */
export function useSeatSwitch({ apply, onSwitched, showToast, isCodexEnabled, onAsk }: UseSeatSwitchOptions) {
  const [busy, setBusy] = useState(false);
  const [ask, setAsk] = useState<{ seatType: SeatType; message: string } | null>(null);

  const openAsk = (seatType: SeatType, message: string) => {
    onAsk?.();
    setAsk({ seatType, message });
  };

  const run = async (seatType: SeatType, confirmation: OverageConfirmation | null) => {
    setBusy(true);
    try {
      await apply(seatType, confirmation);
      setAsk(null);
      onSwitched();
      showToast('席位类型已更新');
    } catch (err) {
      if (err instanceof OverageConfirmationError) {
        // 没带确认，或者服务端没认这次确认（过期、对不上）：都重新问，确认后换一个新的确认。
        openAsk(seatType, err.message);
      } else if (err instanceof OverageForbiddenError || (err instanceof ApiError && err.status === 409)) {
        setAsk(null);
        showToast(err.message, 'error');
      } else {
        showToast(seatUpdateErrorMessage(err, seatType, isCodexEnabled), 'error');
      }
    } finally {
      setBusy(false);
    }
  };

  const pick = (seatType: SeatType, gate: SeatGate | null) => {
    if (gate?.action === 'forbid') {
      showToast(gateMessage(gate) ?? '席位已满，这个 Team 禁止超员', 'error');
      return;
    }
    if (gate?.action === 'confirm') {
      openAsk(seatType, switchConfirmText(gate));
      return;
    }
    void run(seatType, null);
  };

  const label = ask ? SEAT_TYPES[ask.seatType].label : '';
  const dialog = (
    <ConfirmDialog
      open={ask !== null}
      onOpenChange={(open) => {
        if (!open && !busy) setAsk(null);
      }}
      title={`切换到 ${label} 需要加购`}
      message={ask?.message ?? ''}
      confirmLabel="加购并切换"
      destructive
      loading={busy}
      onConfirm={() => {
        // 确认只管这一次切换：1 个这个类型的席位。
        if (ask) void run(ask.seatType, newOverageConfirmation(ask.seatType, 1));
      }}
    />
  );

  return { pick, busy, dialog };
}
