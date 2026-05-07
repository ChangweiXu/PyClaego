import type { KVTableSchema } from '../schema/types';

export function KVTable({ rows }: KVTableSchema) {
  if (!rows.length) return <div className="p-kv-empty">No data</div>;
  return (
    <table className="p-kv-table">
      <tbody>
        {rows.map(([k, v], i) => (
          <tr key={i} className="p-kv-row">
            <td className="p-kv-key">{k}</td>
            <td className="p-kv-val">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
