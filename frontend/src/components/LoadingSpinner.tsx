interface LoadingSpinnerProps {
  size?: number;
  overlay?: boolean;
}

export default function LoadingSpinner({ size = 32, overlay = false }: LoadingSpinnerProps) {
  const spinner = (
    <svg
      className="animate-spin text-blue-500"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
    >
      <circle
        className="opacity-25"
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="4"
      />
      <path
        className="opacity-75"
        fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
      />
    </svg>
  );

  if (overlay) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-black/40 rounded-lg z-10">
        {spinner}
      </div>
    );
  }

  return spinner;
}
