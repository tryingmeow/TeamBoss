import type { SeatType } from '../types';
import { SEAT_TYPES } from '../lib/seatType';
import { cn } from '../lib/utils';
import { PILL, TONE } from './ui';

interface SeatBetaBadgeProps {
  seatType: SeatType;
  className?: string;
}

/**
 * Small neutral 「Beta」 chip placed after a seat type that is still in beta
 * (`SEAT_TYPES[type].beta`); renders nothing for any other type, so a list of seat
 * options can drop it after every label. Gray on purpose: seat colors and status
 * tones already mean something.
 */
export default function SeatBetaBadge({ seatType, className }: SeatBetaBadgeProps) {
  const info = SEAT_TYPES[seatType];
  if (!info.beta) return null;
  return (
    <span
      className={cn(PILL, TONE.neutral, 'px-1 py-0 text-[10px] font-semibold leading-4', className)}
      title={`${info.label} 是 Beta 功能，还没在生产环境中测试过`}
    >
      Beta
    </span>
  );
}
