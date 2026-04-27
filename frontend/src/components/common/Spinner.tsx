interface Props {
  size?: 'sm' | 'md';
  className?: string;
}

export function Spinner({ size = 'md', className = '' }: Props) {
  const dim = size === 'sm' ? 'h-3.5 w-3.5 border-[1.5px]' : 'h-5 w-5 border-2';
  return (
    <div
      className={`animate-spin rounded-full border-gray-200 dark:border-gray-700 border-t-blue-400 ${dim} ${className}`}
    />
  );
}
