export type DeleteChoice = {
  id: string;
  name: string;
  day: number;
  time: string;
  plan_style: string;
};

function normalizedPlace(value: string, destination: string): string {
  let name = value.replace(/[（(][^）)]*[）)]/g, '')
    .replace(/[\s·•「」『』]/g, '')
    .replace(/(?:旅游景区|风景名胜区|风景区|景区)$/g, '');
  const city = destination.replace(/[\s市县区]/g, '');
  if (city && name.startsWith(city)) name = name.slice(city.length);
  return name;
}

export function matchingSpots<T extends { title: string }>(query: string, destination: string, spots: T[]): T[] {
  const wanted = normalizedPlace(query, destination);
  if (!wanted) return [];
  return spots.filter(spot => normalizedPlace(spot.title, destination) === wanted);
}

export function addTimeRange(value: string): string | null {
  if (!value.trim()) return '';
  const match = /^(\d{1,2}):(\d{2})(?:\s*[-—~至]\s*(\d{1,2}):(\d{2}))?$/.exec(value.trim());
  if (!match) return null;
  const startHour = Number(match[1]);
  const startMinute = Number(match[2]);
  const endHour = match[3] == null ? startHour + 1 : Number(match[3]);
  const endMinute = match[4] == null ? startMinute : Number(match[4]);
  const start = startHour * 60 + startMinute;
  const end = endHour * 60 + endMinute;
  if (startHour >= 24 || startMinute >= 60 || endHour > 24 || endMinute >= 60 ||
      (endHour === 24 && endMinute !== 0) || end <= start || end > 1440) return null;
  const clock = (hour: number, minute: number) => `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  return `${clock(startHour, startMinute)}-${clock(endHour, endMinute)}`;
}

export function ambiguousDeleteChoices(error: unknown): DeleteChoice[] | null {
  const message = error instanceof Error ? error.message : '';
  const match = /^请求失败 \(422\): (.+)$/s.exec(message);
  if (!match) return null;
  try {
    const body = JSON.parse(match[1]);
    const detail = body?.detail;
    if (detail?.code !== 'ambiguous_delete' || !Array.isArray(detail.candidates)) return null;
    const choices = detail.candidates.filter((value: unknown): value is DeleteChoice => {
      if (!value || typeof value !== 'object') return false;
      const candidate = value as Record<string, unknown>;
      return typeof candidate.id === 'string' && !!candidate.id &&
        typeof candidate.name === 'string' && Number.isInteger(candidate.day) &&
        typeof candidate.time === 'string' && typeof candidate.plan_style === 'string';
    });
    return choices.length > 1 ? choices : null;
  } catch {
    return null;
  }
}

export function mutationErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  const match = /^请求失败 \(\d+\): (.+)$/s.exec(message);
  if (!match) return message;
  try {
    const detail = JSON.parse(match[1])?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : message;
  } catch {
    return message;
  }
}
