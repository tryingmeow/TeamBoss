import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowUpRight } from 'lucide-react';
import { getFinanceInvoices, type FinanceInvoicesResponse, type FinancePaidAmounts } from '../api/client';
import type { Team } from '../types';
import { formatMoney, sameCurrency, teamUnit } from '../lib/money';
import DialogFrame from './DialogFrame';
import { InvoiceSubTable, formatInvoicePeriod, invoiceStatusCell } from './InvoiceTable';
import { BUTTON } from './ui';

/** Enough for the whole history of any Team so far; the summary always covers every invoice. */
const INVOICE_LIMIT = 100;

type LoadState = FinanceInvoicesResponse | 'loading' | 'error';

function SummaryTile({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-lg bg-gray-50 px-3.5 py-3 dark:bg-ink-950/60">
      <div className="text-xs text-gray-500 dark:text-ink-400">{label}</div>
      <div className="mt-1 text-base font-semibold tabular-nums text-gray-900 dark:text-gray-100">{children}</div>
      {hint && <div className="mt-0.5 text-xs text-gray-500 dark:text-ink-400">{hint}</div>}
    </div>
  );
}

/** Paid amounts in their own currencies, then "≈ base" when any of them was converted (finance-page style). */
function PaidValue({ paid, team, baseCurrency }: { paid: FinancePaidAmounts; team: Team; baseCurrency: string }) {
  if (paid.amounts.length === 0) return <span className="font-normal text-gray-400 dark:text-ink-500">—</span>;
  const converted = paid.base !== null && paid.amounts.some((item) => !sameCurrency(item.currency, baseCurrency));
  return (
    <>
      <span className="break-words">
        {paid.amounts.map((item) => formatMoney(item.amount, teamUnit(team, item.currency))).join(' + ')}
      </span>
      {converted && (
        <span className="block text-xs font-normal text-gray-500 dark:text-ink-400">
          ≈ {formatMoney(paid.base, baseCurrency)}
        </span>
      )}
    </>
  );
}

/** Read-only billing history and spend for one Team, opened from the card's payment-card row. */
export default function TeamBillingDialog({
  open,
  onOpenChange,
  team,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  team: Team;
}) {
  const navigate = useNavigate();
  const [state, setState] = useState<LoadState>('loading');
  // The footer's first button leaves the page; land focus on 「关闭」 so Enter right after opening just closes.
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setState('loading');
    // refresh=false: only what sync already stored, never an upstream fetch from this dialog.
    getFinanceInvoices(team.id, { limit: INVOICE_LIMIT, refresh: false })
      .then((res) => { if (!cancelled) setState(res); })
      .catch(() => { if (!cancelled) setState('error'); });
    return () => {
      cancelled = true;
    };
  }, [open, team.id]);

  const data = typeof state === 'object' ? state : null;
  const summary = data?.summary;
  const latest = summary?.latest_invoice ?? null;
  const baseCurrency = summary?.base_currency || 'USD';
  const latestConverted = latest !== null
    && latest.display_amount_base !== null
    && !sameCurrency(latest.currency, baseCurrency);
  const shown = data?.invoices.length ?? 0;

  const openFinance = () => {
    onOpenChange(false);
    navigate(`/admin/finance?team=${encodeURIComponent(team.id)}`);
  };

  return (
    <DialogFrame
      open={open}
      onOpenChange={onOpenChange}
      size="xl"
      title={`账单 · ${team.name}`}
      description="Stripe 账单和花费，只读已同步的数据。"
      onOpenAutoFocus={(event) => {
        event.preventDefault();
        closeRef.current?.focus();
      }}
      footer={
        <>
          <button type="button" onClick={openFinance} className={BUTTON.secondary}>
            在财务页查看 <ArrowUpRight size={14} />
          </button>
          <button ref={closeRef} type="button" onClick={() => onOpenChange(false)} className={BUTTON.primary}>
            关闭
          </button>
        </>
      }
    >
      {summary && summary.invoice_count > 0 && (
        <div className="mb-4">
          <div className="grid gap-2.5 sm:grid-cols-3">
            <SummaryTile label="累计实付" hint={`已支付 ${summary.paid_count} 期`}>
              <PaidValue paid={summary.paid_total} team={team} baseCurrency={baseCurrency} />
            </SummaryTile>
            <SummaryTile label="近 30 天实付">
              <PaidValue paid={summary.paid_last_30_days} team={team} baseCurrency={baseCurrency} />
            </SummaryTile>
            <SummaryTile
              label="最新一期"
              hint={latest && <span className="inline-flex items-center gap-1.5">{formatInvoicePeriod(latest)} · {invoiceStatusCell(latest.status)}</span>}
            >
              {latest ? (
                <>
                  {formatMoney(latest.display_amount, teamUnit(team, latest.currency))}
                  {latestConverted && (
                    <span className="block text-xs font-normal text-gray-500 dark:text-ink-400">
                      ≈ {formatMoney(latest.display_amount_base, baseCurrency)}
                    </span>
                  )}
                </>
              ) : (
                <span className="font-normal text-gray-400 dark:text-ink-500">—</span>
              )}
            </SummaryTile>
          </div>
          <p className="mt-2 text-pretty text-xs leading-5 text-gray-500 dark:text-ink-400">
            只把已支付的账单算进合计；作废和未支付的照常列出，不计入。
          </p>
        </div>
      )}

      {summary && summary.invoice_count > shown && (
        <p className="mb-1 text-xs text-gray-500 dark:text-ink-400">最近 {shown} 期，共 {summary.invoice_count} 期</p>
      )}
      <div className="-mx-1 overflow-x-auto px-1">
        <InvoiceSubTable state={data ? data.invoices : state === 'error' ? 'error' : 'loading'} />
      </div>
    </DialogFrame>
  );
}
