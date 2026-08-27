interface ExpiryPickerProps {
  value: string;
  onChange: (value: string) => void;
}

const OPTIONS = ['3m', '7d', '14d', '30d', '31d', '180d', '360d', '永不'];

export default function ExpiryPicker({ value, onChange }: ExpiryPickerProps) {
  return (
    <div className="grid grid-cols-4 gap-2">
      {OPTIONS.map((opt) => (
        <button
          key={opt}
          type="button"
          onClick={() => onChange(opt)}
          className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-all ${
            value === opt
              ? 'bg-blue-600 text-white shadow-md shadow-blue-500/20'
              : 'bg-gray-100 dark:bg-[#2a2d3a] text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-[#3a3d4a]'
          }`}
        >
          {opt}
        </button>
      ))}
    </div>
  );
}
