'use client';
import { useMemo, useState } from 'react';

export default function PoolsPage() {
  // Placeholder pagination (10 per page)
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const total = 0; // replace with real count
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  const items = useMemo(() => [], [page]);

  return (
    <main style={{ padding: 24 }}>
      <h1>Pools</h1>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button disabled={page === 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
          Prev
        </button>
        <span>
          Page {page} / {totalPages}
        </span>
        <button disabled={page === totalPages} onClick={() => setPage((p) => Math.min(totalPages, p + 1))}>
          Next
        </button>
      </div>
      <div style={{ marginTop: 16 }}>
        {/* Table placeholder */}
        <div>No pools loaded yet.</div>
      </div>
    </main>
  );
}


