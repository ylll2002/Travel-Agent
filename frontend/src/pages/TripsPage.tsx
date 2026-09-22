import { useState } from 'react';
import type { ChangeEvent, FormEvent } from 'react';
import { api } from '../api/client';
import { TripCard } from '../components/TripCard';
import { useApi } from '../hooks/useApi';

const emptyForm = {
  title: '',
  destination: '',
  start_date: '',
  end_date: '',
};

export function TripsPage() {
  const { data: trips, loading, error, refetch } = useApi(api.listTrips);
  const [form, setForm] = useState(emptyForm);
  const [submitting, setSubmitting] = useState(false);

  function handleChange(e: ChangeEvent<HTMLInputElement>) {
    setForm((prev) => ({ ...prev, [e.target.name]: e.target.value }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    try {
      await api.createTrip(form);
      setForm(emptyForm);
      refetch();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section>
      <h1>我的行程</h1>

      <form onSubmit={handleSubmit} className="trip-form">
        <input
          name="title"
          placeholder="行程标题"
          value={form.title}
          onChange={handleChange}
          required
        />
        <input
          name="destination"
          placeholder="目的地"
          value={form.destination}
          onChange={handleChange}
          required
        />
        <input
          name="start_date"
          type="date"
          value={form.start_date}
          onChange={handleChange}
          required
        />
        <input
          name="end_date"
          type="date"
          value={form.end_date}
          onChange={handleChange}
          required
        />
        <button type="submit" disabled={submitting}>
          创建行程
        </button>
      </form>

      {loading && <p>加载中…</p>}
      {error && <p className="error">{error}</p>}
      <div className="card-grid">
        {trips?.map((trip) => (
          <TripCard key={trip.id} trip={trip} />
        ))}
      </div>
    </section>
  );
}

