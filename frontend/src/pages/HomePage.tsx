import { api } from '../api/client';
import { useApi } from '../hooks/useApi';

export function HomePage() {
  const { data: destinations, loading, error } = useApi(api.listDestinations);

  return (
    <section>
      <h1>探索目的地</h1>
      {loading && <p>加载中…</p>}
      {error && <p className="error">{error}</p>}
      <div className="card-grid">
        {destinations?.map((d) => (
          <article key={d.id} className="card">
            <h2>{d.name}</h2>
            <p className="muted">{d.country}</p>
            <p>{d.description}</p>
            <p className="tags">{d.tags}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

