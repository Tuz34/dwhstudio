import { useState, useEffect, useCallback } from 'react';
// Export must match import in DataOperations: { Kms }
import { getOdiKms } from '../../services/api';

export function Kms() {
  const [kms, setKms] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadKMs = useCallback(async () => {
    try {
      setLoading(true);
      const data = await getOdiKms();
      setKms(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const id = setTimeout(loadKMs, 0);
    return () => clearTimeout(id);
  }, [loadKMs]);

  if (loading) return <div className="text-slate-400 p-8 flex items-center justify-center"><i className="fa-solid fa-spinner fa-spin mr-2"></i> Loading KMs...</div>;
  if (error) return <div className="text-red-500 p-8">Failed to load: {error}</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xl font-bold text-slate-800 flex items-center gap-2">
          <i className="fa-solid fa-microchip text-indigo-500"></i>
          Knowledge Modules
        </h3>
        <button onClick={loadKMs} className="text-slate-400 hover:text-indigo-500 p-2 rounded-full hover:bg-indigo-50 transition-colors">
          <i className="fa-solid fa-arrows-rotate"></i>
        </button>
      </div>

      <div className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm text-slate-600">
            <thead className="text-xs text-slate-500 uppercase bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="px-6 py-3 font-medium">Type</th>
                <th className="px-6 py-3 font-medium">KM Name</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {kms.length === 0 ? (
                <tr><td colSpan="2" className="px-6 py-8 text-center text-slate-500">No records found.</td></tr>
              ) : kms.map((km, i) => (
                <tr key={i} className="hover:bg-slate-50 transition-colors">
                  <td className="px-6 py-3">
                    <span className="bg-indigo-100 text-indigo-700 px-2 py-1 rounded text-xs font-semibold">
                      {km.KM_TYPE}
                    </span>
                  </td>
                  <td className="px-6 py-3 font-medium text-slate-800">{km.KM_NAME}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
