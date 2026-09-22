import type {
  ChatMessage,
  ChatResponse,
  Destination,
  Trip,
  TripCreatePayload,
} from './types';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers ?? {}),
    },
    ...options,
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`请求失败 (${response.status}): ${body}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const api = {
  listTrips: () => request<Trip[]>('/trips'),
  getTrip: (id: number) => request<Trip>(`/trips/${id}`),
  createTrip: (payload: TripCreatePayload) =>
    request<Trip>('/trips', { method: 'POST', body: JSON.stringify(payload) }),
  updateTrip: (id: number, payload: Partial<TripCreatePayload>) =>
    request<Trip>(`/trips/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteTrip: (id: number) =>
    request<void>(`/trips/${id}`, { method: 'DELETE' }),
  listDestinations: () => request<Destination[]>('/destinations'),
  chat: (messages: ChatMessage[]) =>
    request<ChatResponse>('/agent/chat', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),
};

