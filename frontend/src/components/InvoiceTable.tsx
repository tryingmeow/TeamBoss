/**
 * One Team's Stripe invoices (period, status, amounts in the invoice currency, link to the hosted
 * invoice). Shared by the finance page's expanded row and the Team card's billing dialog.
 */
import { format, parseISO } from 'date-fns';
import { ExternalLink, Loader2 } from 'lucide-react';
import type { FinanceInvoiceRow } from '../api/client';
import { formatAmount, formatMoney } from '../lib/money';
import { cn } from '../lib/utils';
import { PILL, TONE } from './ui';

export const INVOICE_STATUS_LABEL: Record<string, string> = {
  paid: '已支付',
  void: '已作废',
  draft: '草稿',
  uncollectible: '无法收款',
};

export function invoiceStatusCell(status: string | null) {
  if (status === 'open') return <span className={cn(PILL, TONE.warning)}>未支付</span>;
  const label = (status && INVOICE_STATUS_LABEL[status]) || status || '—';
  return <span className="text-gray-500 dark:text-ink-400">{label}</span>;
}

/** "MM-dd ~ MM-dd"; with the year when the period crosses a year (a yearly plan would read 04-24 ~ 04-24). */
export function formatInvoicePeriod(row: Pick<FinanceInvoiceRow, 'period_start' | 'period_end'>) {
  if (!row.period_start && !row.period_end) return '—';
  const start = row.period_start ? parseISO(row.period_start) : null;
  const end = row.period_end ? parseISO(row.period_end) : null;
  const pattern = start && end && start.getFullYear() !== end.getFullYear() ? 'yyyy-MM-dd' : 'MM-dd';
  const fmt = (value: Date | null) => (value ? format(value, pattern) : '?');
  return `${fmt(start)} ~ ${fmt(end)}`;
}

// 金额保持原币种，和 Stripe 发票页逐行核对用。
export function InvoiceSubTable({ state }: { state: FinanceInvoiceRow[] | 'loading' | 'error' | undefined }) {
  if (state === undefined || state === 'loading') {
    return (
      <div className="flex items-center gap-2 py-1 text-xs text-gray-500 dark:text-ink-400">
        <Loader2 className="size-3.5 animate-spin" />
        加载账单…
      </div>
    );
  }
  if (state === 'error') {
    return <div className="py-1 text-xs text-red-600 dark:text-red-400">账单加载失败</div>;
  }
  if (state.length === 0) {
    return <div className="py-1 text-xs text-gray-500 dark:text-ink-400">暂无已同步账单</div>;
  }

  const headerCurrency = state[0].currency || '';
  const unit = headerCurrency ? `（${headerCurrency.toUpperCase()}）` : '';
  return (
    <table className="w-full text-xs">
      <thead className="text-gray-500 dark:text-ink-400">
        <tr>
          <th className="whitespace-nowrap py-1.5 pr-3 text-left font-medium">账期</th>
          <th className="whitespace-nowrap py-1.5 pr-3 text-left font-medium">状态</th>
          <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">应付{unit}</th>
          <th className="whitespace-nowrap py-1.5 pr-3 text-right font-medium">实付{unit}</th>
          <th className="py-1.5 pr-3 text-left font-medium">说明</th>
          <th className="py-1.5 text-right font-medium" />
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-200/70 dark:divide-ink-800">
        {state.map(row => {
          const amount = (value: number | null) => {
            if (value === null) return '—';
            return row.currency && row.currency !== headerCurrency ? formatMoney(value, row.currency) : formatAmount(value);
          };
          return (
            <tr key={row.invoice_id} className={row.status === 'void' ? 'opacity-60' : ''}>
              <td className="whitespace-nowrap py-1.5 pr-3 tabular-nums text-gray-700 dark:text-ink-300" title={row.number || undefined}>
                {formatInvoicePeriod(row)}
              </td>
              <td className="whitespace-nowrap py-1.5 pr-3">{invoiceStatusCell(row.status)}</td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-gray-700 dark:text-ink-300">
                {amount(row.amount_due)}
              </td>
              <td className="whitespace-nowrap py-1.5 pr-3 text-right tabular-nums text-gray-700 dark:text-ink-300">
                {amount(row.amount_paid)}
              </td>
              {/* Two lines, not one: the tail ("· promo -$75", "+ proration") is what explains the amount. */}
              <td className="min-w-[12rem] max-w-[22rem] py-1.5 pr-3 text-gray-500 dark:text-ink-400" title={row.description || undefined}>
                <div className="line-clamp-2 break-words">{row.description || '—'}</div>
              </td>
              <td className="py-1.5 text-right">
                {row.hosted_invoice_url && (
                  <a
                    href={row.hosted_invoice_url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={event => event.stopPropagation()}
                    className="inline-flex rounded p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 dark:text-ink-500 dark:hover:bg-ink-800 dark:hover:text-gray-200"
                    title="在 Stripe 查看发票"
                    aria-label="在 Stripe 查看发票"
                  >
                    <ExternalLink className="size-3.5" />
                  </a>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
