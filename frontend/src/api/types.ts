export interface ItineraryItem {
  id: number;
  trip_id: number;
  day: number;
  title: string;
  description: string;
  location: string;
  start_time: string | null;
  end_time: string | null;
}

export interface Trip {
  id: number;
  title: string;
  destination: string;
  start_date: string;
  end_date: string;
  status: string;
  budget: number | null;
  notes: string;
  created_at: string;
  updated_at: string;
  items: ItineraryItem[];
}

export interface Destination {
  id: number;
  name: string;
  country: string;
  description: string;
  tags: string;
}

export interface TripCreatePayload {
  title: string;
  destination: string;
  start_date: string;
  end_date: string;
  status?: string;
  budget?: number | null;
  notes?: string;
  items?: Array<{
    day: number;
    title: string;
    description?: string;
    location?: string;
  }>;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface ChatResponse {
  reply: string;
}

export interface UserProfile {
  age_group: string;
  mbti: string;
  city: string;
  companion: string[];
  pace: string[];
  budget: string[];
  accommodation: string[];
  transport: string[];
  interests: string[];
  dietary: string[];
}

export interface TripInfo {
  destination: string;
  origin: string;
  start_date: string;
  end_date: string;
  travelers: string;
  companions: string[];
  budget_tiers: string[];
  total_budget: string;
  purposes: string[];
  special_needs: string[];
}
