import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';

export function TripDetailPage() {
  const { id } = useParams();
  const tripId = Number(id);
  const { data: trip, loading, error } = useApi(
    () => api.getTrip(tripId),
    [tripId],
  );

  if (loading) return <p>加载中…</p>;
  if (error) return <p className="error">{error}</p>;
  if (!trip) return <p>未找到行程。</p>;

  return (
    <section>
      <Link to="/trips" className="back-link">
        ← 返回行程列表
      </Link>
      <h1>{trip.title}</h1>
      <p className="muted">
        {trip.destination} · {trip.start_date} ~ {trip.end_date}
      </p>
      <p>
        状态：{trip.status} · 预算：{trip.budget ?? '未设置'}
      </p>
      {trip.notes && <p>{trip.notes}</p>}

      <h2>日程安排</h2>
      {trip.items.length === 0 ? (
        <p className="muted">暂无日程，去问问 AI 助手帮你规划吧。</p>
      ) : (
        <ul className="itinerary">
          {trip.items.map((item) => (
            <li key={item.id}>
              <span className="day">Day {item.day}</span>
              <strong>{item.title}</strong>
              <span className="muted">{item.location}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

