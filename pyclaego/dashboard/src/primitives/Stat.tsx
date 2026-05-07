import type { StatSchema } from '../schema/types';

export function Stat({ label, value, trend }: StatSchema) {
  const trendEl =
    trend !== undefined && trend !== null ? (
      <span className={`p-stat-trend ${trend > 0 ? 'up' : trend < 0 ? 'down' : 'flat'}`}>
        {trend > 0 ? '▲' : trend < 0 ? '▼' : '—'}
        {Math.abs(trend).toFixed(1)}%
      </span>
    ) : null;
  return (
    <div className="p-stat">
      <div className="p-stat-label">{label}</div>
      <div className="p-stat-value">{value}</div>
      {trendEl}
    </div>
  );
}
