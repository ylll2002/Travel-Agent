import { Link } from 'react-router-dom';
import type { Trip } from '../api/types';

export function TripCard({ trip }: { trip: Trip }) {
  return (
    <Link to={`/trips/${trip.id}`} className="trip-card">
      <h3>{trip.title}</h3>
      <p className="destination">{trip.destination}</p>
      <p className="dates">
        {trip.start_date} ~ {trip.end_date}
      </p>
      <span className={`status status-${trip.status}`}>{trip.status}</span>
    </Link>
  );
}

