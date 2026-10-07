import { useEffect, useState, type ComponentProps } from 'react';
import { getSeatPurchasePreview, type SeatPurchasePreview } from '../api/client';
import { formatBeijingDateTime } from '../lib/formatDate';
import ConfirmDialog from './ConfirmDialog';
import SeatProductionWarning from './SeatProductionWarning';

export interface SeatPurchaseRequest {
  teamId: string;
  teamName?: string;
  seatType: 'default' | 'prolite';
  additionalSeats: number;
}

type QuoteResult = { request: SeatPurchaseRequest; quote: SeatPurchasePreview | null };
type Props = ComponentProps<typeof ConfirmDialog> & { quoteRequests: SeatPurchaseRequest[] };

function validQuote(quote: SeatPurchasePreview, request: SeatPurchaseRequest): boolean {
  return Boolean(quote && quote.currency && quote.quoted_at
    && Number.isInteger(quote.minor_unit_exponent) && quote.minor_unit_exponent >= 0 && quote.minor_unit_exponent <= 6
    && quote.seat_type === request.seatType && quote.additional_seats === request.additionalSeats
    && ['monthly', 'yearly'].includes(quote.current_recurring?.period)
    && ['monthly', 'yearly'].includes(quote.proposed_recurring?.period)
    && [quote.due_now?.amount, quote.due_now?.tax_amount, quote.current_recurring?.amount,
      quote.current_recurring?.discount, quote.proposed_recurring?.amount, quote.proposed_recurring?.discount]
      .every(value => typeof value === 'number' && Number.isFinite(value)));
}

function quoteMoney(amount: number, quote: SeatPurchasePreview): string {
  return `${new Intl.NumberFormat('en-US', {
    minimumFractionDigits: Number.isInteger(amount) ? 0 : quote.minor_unit_exponent,
    maximumFractionDigits: quote.minor_unit_exponent,
  }).format(amount)} ${quote.currency.toUpperCase()}`;
}

/** One preview per Team when a new confirmation opens. No preview authorizes a purchase. */
export default function SeatPurchaseConfirmDialog({ quoteRequests, children, loading, onConfirm, ...props }: Props) {
  const [state, setState] = useState<{ requests: SeatPurchaseRequest[]; results: QuoteResult[] } | null>(null);
  useEffect(() => {
    if (!props.open || quoteRequests.length === 0) {
      setState(null);
      return;
    }
    const controller = new AbortController();
    let active = true;
    Promise.all(quoteRequests.map(async request => {
      try {
        const quote = await getSeatPurchasePreview(request.teamId, request.seatType, request.additionalSeats, controller.signal);
        return { request, quote: validQuote(quote, request) ? quote : null };
      } catch {
        return { request, quote: null };
      }
    })).then(results => { if (active) setState({ requests: quoteRequests, results }); });
    return () => { active = false; controller.abort(); };
  }, [props.open, quoteRequests]);

  // Identity changes on every new confirmation, including a replan with the same seat counts.
  const pending = props.open && quoteRequests.length > 0 && state?.requests !== quoteRequests;
  const results = state?.requests === quoteRequests ? state.results : [];
  return (
    <ConfirmDialog {...props} loading={loading || pending} onConfirm={() => { if (!pending) onConfirm(); }}>
      <div className="space-y-3" aria-live="polite">
        <SeatProductionWarning />
        {pending ? <p className="text-sm text-gray-500 dark:text-ink-400">正在获取 ChatGPT 即时报价…</p> : results.map(({ request, quote }) => (
          <div key={request.teamId} className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-sm dark:border-ink-700 dark:bg-ink-800/60">
            <p className="mb-2 font-medium text-gray-900 dark:text-gray-100">{request.teamName || '当前 Team'} · 加购 {request.additionalSeats} 席</p>
            {quote ? (
              <>
                <dl className="space-y-1 text-gray-600 dark:text-ink-300">
                  <div className="flex justify-between gap-3 font-semibold text-gray-900 dark:text-gray-100"><dt>本次应付</dt><dd>{quoteMoney(quote.due_now.amount, quote)}</dd></div>
                  <div className="flex justify-between gap-3"><dt>当前每{quote.current_recurring.period === 'yearly' ? '年' : '月'}费用</dt><dd>{quoteMoney(quote.current_recurring.amount, quote)}</dd></div>
                  <div className="flex justify-between gap-3"><dt>调整后每{quote.proposed_recurring.period === 'yearly' ? '年' : '月'}费用</dt><dd>{quoteMoney(quote.proposed_recurring.amount, quote)}</dd></div>
                  <div className="flex justify-between gap-3"><dt>调整后每期折扣（已含）</dt><dd>{quoteMoney(quote.proposed_recurring.discount, quote)}</dd></div>
                </dl>
                <p className="mt-2 text-xs text-gray-500 dark:text-ink-400">{import.meta.env.MODE === 'demo' ? '演示报价（虚构）' : 'ChatGPT 报价'} · {formatBeijingDateTime(quote.quoted_at)}。实际扣款可能随结算时间变化。</p>
              </>
            ) : <p className="text-amber-700 dark:text-amber-400">即时报价暂不可用，本次应付未知。上方仅为席位原价。</p>}
          </div>
        ))}
        {!pending && quoteRequests.length === 0 && <p className="text-sm text-amber-700 dark:text-amber-400">即时报价暂不可用，本次应付未知。上方仅为席位原价。</p>}
      </div>
      {children}
    </ConfirmDialog>
  );
}
