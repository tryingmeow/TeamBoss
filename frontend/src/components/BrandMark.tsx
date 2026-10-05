/** TeamBoss logo mark; public/favicon.svg is the same drawing. */
export default function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" className="shrink-0">
      <rect width="32" height="32" rx="8" className="fill-blue-600" />
      <path d="M9.5 11h13M16 11v11.5" stroke="#fff" strokeWidth="3.4" strokeLinecap="round" fill="none" />
    </svg>
  );
}
