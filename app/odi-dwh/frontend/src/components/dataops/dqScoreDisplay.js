const UNKNOWN_SCORE = { label: 'Unknown', width: '0%', bar: 'bg-slate-300', text: 'text-slate-500' };

function measuredColors(score) {
  if (score > 90) return { bar: 'bg-emerald-500', text: 'text-emerald-600' };
  if (score > 70) return { bar: 'bg-amber-400', text: 'text-amber-600' };
  return { bar: 'bg-rose-500', text: 'text-rose-600' };
}

// A missing score means the table was not fully evaluated, which is not a measured 0.
export function dqScoreDisplay(score) {
  if (typeof score !== 'number' || !Number.isFinite(score)) return { ...UNKNOWN_SCORE };
  return { label: `%${score}`, width: `${score}%`, ...measuredColors(score) };
}
