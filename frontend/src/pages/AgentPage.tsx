import { useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { api } from '../api/client';
import { TripMap } from '../components/TripMap';
import type { RouteBlock, RouteLeg } from '../components/TripMap';
import { TripQuestions } from '../components/TripQuestions';
import type { TripQuestion } from '../components/TripQuestions';
import { usePlanStream } from '../hooks/usePlanStream';
import { advanceReviewProgress, canAutoRepair, confirmationReason, currentAudit, issueTarget, reviewMessage } from '../lib/planReview';
import CityGuide from '../components/CityGuide';
import { PlanReviewWindow } from '../components/PlanReviewWindow';
import type { AuditIssue, PlanAudit, ReviewContext, ReviewProgress } from '../lib/planReview';

// Compact map size follows the final responsive rules in index.css.
// Recompute on opening so the map always starts at the bottom-right.
function getMapDockPosition() {
  if (typeof window === 'undefined') return { x: 760, y: 520 };

  const mobile = window.innerWidth <= 760;
  const width = Math.min(mobile ? 340 : 360, window.innerWidth - (mobile ? 24 : 32));
  const height = mobile
    ? Math.min(292, window.innerHeight - 84)
    : window.innerHeight <= 760
      ? Math.min(300, window.innerHeight - 88)
      : Math.min(318, window.innerHeight - 96);

  return {
    x: Math.max(12, window.innerWidth - width - 24),
    y: Math.max(68, window.innerHeight - height - 10),
  };
}

type ChatMessage = {
  id: string;
  role: 'assistant' | 'user';
  content: string;
  kind?: 'text' | 'trip-confirm';
  tripConfirmData?: Record<string, unknown>;
};

type TripConfirmField = {
  label: string;
  value: string;
  wide?: boolean;
};

type OptionType = 'flight' | 'hotel' | 'spot' | 'event' | 'food';

type BaseOption = {
  id: string;
  title: string;
  subtitle: string;
  scheduleAt: string;
  scheduleLabel: string;
  location: string;
  tags: string[];
  description: string;
  url?: string;
  image?: string;
  lng?: number;
  lat?: number;
  suggestedDay?: number;
  priceKnown?: boolean;
};

type FlightOption = BaseOption & {
  type: 'flight';
  mode: 'flight' | 'train';
  from: string;
  to: string;
  departTime: string;
  arriveTime: string;
  carrier: string;
  code: string;
  duration: string;
  price: number;
};

type HotelOption = BaseOption & {
  type: 'hotel';
  district: string;
  checkIn: string;
  checkOut: string;
  roomType: string;
  rating: number;
  nightlyPrice: number;
  totalPrice: number;
};

type SpotOption = BaseOption & {
  type: 'spot';
  area: string;
  openHours: string;
  recommendedDuration: string;
  ticketPrice: number;
};

type EventOption = BaseOption & {
  type: 'event';
  price: number;
};

type FoodOption = BaseOption & {
  type: 'food';
  cuisine: string;
  rating: number;
  pricePerPerson: number;
  businessArea: string;
  address: string;
  detailUrl: string;
  mapUrl: string;
  distanceM?: number;
  walkingDistanceM?: number;
  walkingDurationS?: number;
  walkingOrigin?: string;
};

type OptionItem = FlightOption | HotelOption | SpotOption | EventOption | FoodOption;

function parsePrice(value: unknown): number {
  return knownPrice(value) ?? 0;
}

function knownPrice(value: unknown): number | null {
  const text = String(value ?? '').trim().replace(/^(?:¥|￥|RMB|CNY)\s*/i, '').replace(/\s*元$/, '').replace(/,/g, '');
  if (!/^\d+(?:\.\d+)?$/.test(text)) return null;
  const number = Number(text);
  return Number.isFinite(number) ? number : null;
}

type RoutePlan = {
  revision?: number;
  audit?: PlanAudit | null;
  history?: Array<Record<string, unknown>>;
  review_pending?: boolean;
  review_context?: ReviewContext;
  basic?: Record<string, unknown>;
  destination: string;
  start_date: string;
  end_date: string;
  total_cost?: number;
  budget_status?: string;
  cost_by_style?: Record<string, number>;
  budget_by_style?: Record<string, string>;
  unpriced_items?: Record<string, string[]>;
  styles: string[];
  summaries: Record<string, string>;
  blocks: RouteBlock[];
  legs: RouteLeg[];
  suggestions?: PlanSuggestions;
};

type SuggestionCard = {
  name: string;
  image: string;
  subtitle: string;
  link: string;
};

type PlanSuggestions = {
  cover_image: string;
  spots_rank: SuggestionCard[];
  spots_match: SuggestionCard[];
  restaurants: SuggestionCard[];
  hotels: SuggestionCard[];
};

function getPlanDayCount(plan: RoutePlan): number {
  const dateDays = Math.round((Date.parse(plan.end_date) - Date.parse(plan.start_date)) / 86400000) + 1;
  const blockDays = plan.blocks.map((block) => Number(block.day)).filter(Number.isFinite);
  return Math.max(1, Number.isFinite(dateDays) ? dateDays : 1, ...blockDays);
}

function transportRows(items: unknown): unknown[] {
  if (Array.isArray(items)) return items;
  const groups = items as { outbound?: unknown; inbound?: unknown } | null;
  return [groups?.outbound, groups?.inbound].flatMap((group) => Array.isArray(group) ? group : []);
}

function mapFlights(items: unknown): FlightOption[] {
  const list = transportRows(items);
  return list.slice(0, 10).map((f: any, i: number) => ({
    id: `flight-${i}`,
    type: 'flight',
    mode: 'flight',
    title: `${f?.airline ?? ''}${f?.flight_no ?? ''}`,
    subtitle: `${f?.dep_station ?? ''} → ${f?.arr_station ?? ''}`,
    from: f?.dep_station ?? '',
    to: f?.arr_station ?? '',
    departTime: f?.dep_time ?? '',
    arriveTime: f?.arr_time ?? '',
    carrier: f?.airline ?? '',
    code: f?.flight_no ?? '',
    duration: f?.duration ?? '',
    price: parsePrice(f?.price),
    priceKnown: knownPrice(f?.price) != null,
    scheduleAt: f?.dep_time ?? '',
    scheduleLabel: f?.dep_time ?? '',
    location: f?.arr_station ?? '',
    tags: f?.seat ? [f.seat] : [],
    description: `${f?.airline ?? ''}${f?.flight_no ?? ''}，${f?.dep_station ?? ''} → ${f?.arr_station ?? ''}`,
    url: f?.url ?? '',
  }));
}

function mapTrains(items: unknown): FlightOption[] {
  const list = transportRows(items);
  return list.slice(0, 10).map((t: any, i: number) => ({
    id: `train-${i}`,
    type: 'flight',
    mode: 'train',
    title: `${t?.transport ?? ''}${t?.train_no ?? ''}`,
    subtitle: `${t?.dep_station ?? ''} → ${t?.arr_station ?? ''}`,
    from: t?.dep_station ?? '',
    to: t?.arr_station ?? '',
    departTime: t?.dep_time ?? '',
    arriveTime: t?.arr_time ?? '',
    carrier: t?.transport ?? '',
    code: t?.train_no ?? '',
    duration: t?.duration ?? '',
    price: parsePrice(t?.price),
    priceKnown: knownPrice(t?.price) != null,
    scheduleAt: t?.dep_time ?? '',
    scheduleLabel: t?.dep_time ?? '',
    location: t?.arr_station ?? '',
    tags: t?.seat ? [t.seat] : [],
    description: `${t?.transport ?? ''}${t?.train_no ?? ''}，${t?.dep_station ?? ''} → ${t?.arr_station ?? ''}`,
    url: t?.url ?? '',
  }));
}

function mapHotels(items: unknown): HotelOption[] {
  return (Array.isArray(items) ? items : []).slice(0, 30).map((h: any, i: number) => {
    const price = parsePrice(h?.price);
    return {
      id: `hotel-${i}`,
      type: 'hotel',
      title: h?.name ?? '',
      subtitle: h?.location ?? '',
      district: h?.location ?? '',
      checkIn: '',
      checkOut: '',
      roomType: h?.star ?? '',
      rating: parseFloat(String(h?.score ?? 0)) || 0,
      nightlyPrice: price,
      totalPrice: price,
      priceKnown: knownPrice(h?.price) != null,
      scheduleAt: '',
      scheduleLabel: '',
    location: h?.location ?? '',
    tags: h?.star ? [h.star] : [],
    description: `${h?.name ?? ''}，${h?.star ?? ''}，${h?.location ?? ''}`,
    url: h?.url ?? '',
    image: h?.image ?? '',
    lng: h?.longitude != null ? Number(h.longitude) : undefined,
    lat: h?.latitude != null ? Number(h.latitude) : undefined,
  };
  });
}

function mapSpots(items: unknown): SpotOption[] {
  return (Array.isArray(items) ? items : []).slice(0, 50).map((p: any, i: number) => ({
    id: `spot-${i}`,
    type: 'spot',
    title: p?.name ?? '',
    subtitle: p?.category ?? '',
    area: p?.district_label ?? p?.category ?? '',
    openHours: '',
    recommendedDuration: p?.duration ?? '',
    ticketPrice: parsePrice(p?.price ?? p?.ticket_price),
    priceKnown: p?.free === true || knownPrice(p?.price ?? p?.ticket_price) != null,
    scheduleAt: '',
    scheduleLabel: '',
    location: '',
    tags: p?.rank ? [p.rank] : [],
    description: p?.description ?? '',
    url: p?.url ?? '',
    image: p?.image ?? '',
    lng: p?.longitude != null ? Number(p.longitude) : undefined,
    lat: p?.latitude != null ? Number(p.latitude) : undefined,
  }));
}

function mapEvents(items: unknown): EventOption[] {
  return (Array.isArray(items) ? items : []).slice(0, 20).map((e: any, i: number) => ({
    id: `event-${i}`,
    type: 'event',
    title: e?.title ?? '',
    subtitle: e?.content ?? '',
    scheduleAt: '',
    scheduleLabel: '',
    location: '',
    tags: [],
    description: e?.content ?? '',
    price: 0,
    priceKnown: false,
    url: e?.url ?? '',
  }));
}

function distanceNumber(value: unknown): number | undefined {
  if (value == null || value === '') return undefined;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : undefined;
}

function formatWalkingInfo(distance: number | null | undefined, duration: number | null | undefined): string {
  const meters = distanceNumber(distance);
  if (meters == null) return '';
  const seconds = distanceNumber(duration);
  return `步行约${Math.round(meters)}米${seconds != null ? ` / ${Math.ceil(seconds / 60)}分钟` : ''}`;
}

function formatLegSummary(leg: RouteLeg | undefined): string {
  if (!leg?.options?.length) return '';
  return leg.options.map((o) => {
    const minutes = Math.max(1, Math.round(o.duration_s / 60));
    const price = o.price != null ? ` ¥${o.price}` : '';
    return `${o.label} ${minutes}分钟${price}`;
  }).join(' · ');
}

function mapFood(items: unknown): FoodOption[] {
  return (Array.isArray(items) ? items : []).slice(0, 200).map((f: any, i: number) => ({
    id: `food-${i}`,
    type: 'food',
    title: f?.name ?? f?.title ?? '',
    subtitle: f?.cuisine ?? f?.business_area ?? '',
    cuisine: f?.cuisine ?? '',
    rating: parseFloat(String(f?.rating ?? 0)) || 0,
    pricePerPerson: parsePrice(f?.price_per_person ?? f?.price),
    priceKnown: knownPrice(f?.price_per_person ?? f?.price) != null,
    businessArea: f?.business_area ?? '',
    address: f?.address ?? '',
    detailUrl: f?.poi_detail_url ?? f?.url ?? '',
    mapUrl: f?.map_url ?? '',
    url: f?.poi_detail_url ?? f?.map_url ?? f?.url ?? '',
    lng: f?.longitude != null ? Number(f.longitude) : undefined,
    lat: f?.latitude != null ? Number(f.latitude) : undefined,
    suggestedDay: f?.day != null ? Number(f.day) : undefined,
    distanceM: distanceNumber(f?.distance_m),
    walkingDistanceM: distanceNumber(f?.walking_distance_m),
    walkingDurationS: distanceNumber(f?.walking_duration_s),
    walkingOrigin: typeof f?.walking_origin === 'string' ? f.walking_origin : undefined,
    scheduleAt: '',
    scheduleLabel: f?.day ? `第${f.day}天${f?.meal ?? ''}附近` : '',
    location: f?.business_area ?? f?.address ?? '',
    tags: [f?.cuisine, f?.business_area, f?.rating ? `${f.rating}分` : ''].filter(Boolean),
    description: f?.address ?? f?.content ?? '',
  }));
}

const formatPrice = (price: number) => `¥ ${price.toLocaleString()}`;
const buildId = () => `${Date.now()}-${Math.random()}`;

function formatTripDate(value: unknown): string {
  const text = String(value ?? '').trim();
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(text);
  if (!match) return text;
  return `${Number(match[2])}月${Number(match[3])}日`;
}

function formatTravelers(value: unknown): string {
  const text = String(value ?? '').trim();
  if (!text) return '';
  return /人$/.test(text) ? text : `${text} 人`;
}

function getTripConfirmFields(data: Record<string, unknown>): TripConfirmField[] {
  const fields: TripConfirmField[] = [];

  if (data.destination) fields.push({ label: '目的地', value: String(data.destination) });
  if (data.origin) fields.push({ label: '出发地', value: String(data.origin) });
  if (data.start_date) fields.push({ label: '出发日期', value: formatTripDate(data.start_date) });
  if (data.end_date) fields.push({ label: '返程日期', value: formatTripDate(data.end_date) });
  if (data.travelers) fields.push({ label: '出行人数', value: formatTravelers(data.travelers) });

  if (data.budget_unlimited) {
    fields.push({ label: '总预算', value: '不设限' });
  } else if (data.total_budget) {
    const amount = Number(data.total_budget);
    fields.push({
      label: '总预算',
      value: Number.isFinite(amount) ? `¥${amount.toLocaleString()}` : String(data.total_budget),
    });
  }

  if (Array.isArray(data.purposes) && data.purposes.length) {
    fields.push({
      label: '兴趣',
      value: (data.purposes as unknown[]).map(String).join('、'),
    });
  }
  if (Array.isArray(data.requested_pois) && data.requested_pois.length) {
    fields.push({
      label: '指定景点',
      value: (data.requested_pois as unknown[]).map(String).join('、'),
      wide: true,
    });
  }
  if (data.food_keyword) fields.push({ label: '餐饮偏好', value: String(data.food_keyword) });
  if (data.notes) fields.push({ label: '其他要求', value: String(data.notes), wide: true });

  return fields;
}

function buildTripConfirmContent(data: Record<string, unknown>): string {
  const details = getTripConfirmFields(data).map((field) => `${field.label}：${field.value}`);
  return ['请确认本次旅行信息：', ...details, '确认无误后开始规划，也可以继续补充或修改。'].join('\n');
}

function getOptionPrice(item: OptionItem) {
  if (item.type === 'flight') return item.price;
  if (item.type === 'hotel') return item.totalPrice;
  if (item.type === 'event') return item.price;
  if (item.type === 'food') return item.pricePerPerson;
  return item.ticketPrice;
}

function getOptionIcon(item: OptionItem) {
  if (item.type === 'flight') return item.mode === 'train' ? '🚄' : '✈';
  if (item.type === 'hotel') return '▣';
  if (item.type === 'event') return '♪';
  if (item.type === 'food') return '食';
  return '●';
}

function getOptionTypeLabel(type: OptionType) {
  if (type === 'flight') return '出行';
  if (type === 'hotel') return '酒店';
  if (type === 'event') return '活动';
  if (type === 'food') return '美食';
  return '景点';
}

const AIRLINE_ALIAS: Record<string, string> = {
  春秋航空: '春秋',
  东方航空: '东航',
  中国国航: '国航',
  中国南方航空: '南航',
  海南航空: '海航',
  四川航空: '川航',
  吉祥航空: '吉祥',
  厦门航空: '厦航',
  深圳航空: '深航',
};

function getCarrierBadge(item: OptionItem) {
  if (item.type !== 'flight') return '';
  if (item.mode === 'train') return '高铁';
  return AIRLINE_ALIAS[item.carrier] ?? (item.carrier.slice(0, 2) || '航班');
}

export function AgentPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: buildId(),
      role: 'assistant',
      content:
        '你好，直接告诉我你的旅行想法就好，例如“下周五从上海去杭州玩3天，2个人，总预算5000元，想逛西湖、吃杭帮菜”。缺少的必要信息我会再请你补充。',
    },
  ]);

  const [draft, setDraft] = useState('');
  const composerRef = useRef<HTMLTextAreaElement | null>(null);
  const [activeTab, setActiveTab] = useState<OptionType>('flight');
  const [batchIndex, setBatchIndex] = useState<Record<string, number>>({});
  const [socialBatch, setSocialBatch] = useState(0);
  const [planItemIds, setPlanItemIds] = useState<string[]>([]);
  const [detailItem, setDetailItem] = useState<OptionItem | null>(null);
  const [options, setOptions] = useState<OptionItem[]>([]);
  const [socialFood, setSocialFood] = useState<
    Array<{ platform: string; title: string; url: string }>
  >([]);
  const [pendingAdd, setPendingAdd] = useState<OptionItem | null>(null);
  const [tripData, setTripData] = useState<Record<string, unknown>>({});
  const [collectingTrip, setCollectingTrip] = useState(false);
  const [pendingConfirm, setPendingConfirm] = useState<{
    summary: string;
    mode: string;
    targets?: unknown[];
    instruction: string;
  } | null>(null);
  const [routePlan, setRoutePlan] = useState<RoutePlan | null>(null);
  const [reviewLocation, setReviewLocation] = useState<ReturnType<typeof issueTarget>>(null);
  const activityRefs = useRef(new Map<string, HTMLDivElement>());
  const dayRefs = useRef(new Map<number, HTMLDivElement>());
  const [activeStyle, setActiveStyle] = useState('');
  const [activeDay, setActiveDay] = useState<number | 'all'>('all');
  const [expandedStyle, setExpandedStyle] = useState<string | null>(null);
  const [confirmedStyle, setConfirmedStyle] = useState<string | null>(null);
  const [planRating, setPlanRating] = useState<number | null>(null);
  const [planFeedback, setPlanFeedback] = useState('');
  const [confirmModalOpen, setConfirmModalOpen] = useState(false);
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [question, setQuestion] = useState<{
    question: string;
    questions: TripQuestion[];
  } | null>(null);
  const [tripConfirm, setTripConfirm] = useState<Record<string, unknown> | null>(null);
  const [weatherData, setWeatherData] = useState<{ days?: Array<Record<string, unknown>> } | null>(null);
  const [selectedBlocks, setSelectedBlocks] = useState<Set<string>>(new Set());
  const [addDay, setAddDay] = useState(1);
  const [addTime, setAddTime] = useState('');
  const lastSearchRef = useRef<Record<string, unknown>>({});
  const intentVersionRef = useRef(0);
  const planRequestVersionRef = useRef(0);
  const committedSearchRef = useRef<Record<string, unknown>>({});
  const requestControllerRef = useRef<(AbortController & { mutation?: boolean }) | null>(null);
  const [planning, setPlanning] = useState(false);
  const [generation, setGeneration] = useState<{id:number; destination:string; progress:number; status:'running'|'complete'|'stopped'} | null>(null);
  useEffect(() => {
    if (!planning) setGeneration(previous => previous?.status === 'running' ? {...previous, status:'stopped'} : previous);
  }, [planning]);
  useEffect(() => {
    if (!planning) return;
    const timer = window.setInterval(() => setGeneration(previous => previous?.status === 'running'
      ? {...previous, progress:Math.max(previous.progress, Math.min(94, previous.progress + 1))} : previous), 5000);
    return () => window.clearInterval(timer);
  }, [planning]);
  const [reviewProgress, setReviewProgress] = useState<ReviewProgress | null>(null);
  const [mapPosition, setMapPosition] = useState(getMapDockPosition);
  const [isMapExpanded, setIsMapExpanded] = useState(false);
  // Both floating windows start folded, as small tabs at the right edge.
  const [isMapHidden, setIsMapHidden] = useState(true);
  const mapDragRef = useRef<{ offsetX: number; offsetY: number } | null>(null);
  const { start: startPlanStream, stop: stopPlanStream } = usePlanStream();

  const confirmDisabledReason = confirmationReason(routePlan, planning, selectedBlocks.size);
  useEffect(() => {
    setReviewLocation(null);
    setConfirmModalOpen(false);
  }, [routePlan?.revision, routePlan?.audit]);
  useEffect(() => {
    if (!reviewLocation || expandedStyle !== reviewLocation.style) return;
    const element = reviewLocation.ids.length
      ? activityRefs.current.get(reviewLocation.ids[0]) : dayRefs.current.get(reviewLocation.day);
    element?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    element?.focus({ preventScroll: true });
  }, [reviewLocation, expandedStyle]);

  function locateReviewIssue(issue: AuditIssue) {
    if (!routePlan || planning) return;
    const target = issueTarget(issue, routePlan.blocks);
    if (!target) return;
    setActiveStyle(target.style);
    setExpandedStyle(target.style);
    setActiveDay(target.day);
    setReviewLocation(target);
  }

  async function retryReview() {
    if (!routePlan || planning || !routePlan.revision) return;
    const snapshot = routePlan;
    setSelectedBlocks(new Set());
    const version = ++planRequestVersionRef.current;
    setPlanning(true);
    addAssistantMessage('正在审核当前行程，发现可修复的问题时将自动调整…');
    try {
      const audit = await repairCurrentSnapshot(snapshot, committedSearchRef.current, version);
      if (version === planRequestVersionRef.current) updateLastAssistantMessage(reviewMessage(audit));
    } finally {
      if (version === planRequestVersionRef.current) setPlanning(false);
    }
  }

  const styleBlocks = useMemo(
    () => routePlan?.blocks.filter((b) => b.plan_style === activeStyle) ?? [],
    [routePlan, activeStyle],
  );
  const styleLegs = useMemo(
    () => routePlan?.legs.filter((leg) => leg.plan_style === activeStyle) ?? [],
    [routePlan, activeStyle],
  );
  const expandedLegs = useMemo(
    () => routePlan?.legs.filter((leg) => leg.plan_style === expandedStyle) ?? [],
    [routePlan, expandedStyle],
  );
  const planDays = useMemo(
    () => Array.from(new Set(styleBlocks.map((b) => b.day))).sort((a, b) => a - b),
    [styleBlocks],
  );

  const visibleOptions: OptionItem[] = useMemo(() => {
    if (activeTab === 'flight') return options.filter((o) => o.type === 'flight');
    if (activeTab === 'hotel') {
      const list = options.filter((o) => o.type === 'hotel');
      const start = ((batchIndex[activeTab] ?? 0) * 5) % Math.max(1, list.length);
      return list.slice(start, start + 5);
    }
    if (activeTab === 'spot') {
      const list = options.filter((o) => o.type === 'spot');
      const start = ((batchIndex[activeTab] ?? 0) * 5) % Math.max(1, list.length);
      return list.slice(start, start + 5);
    }
    if (activeTab === 'event') {
      const list = options.filter((o) => o.type === 'event');
      const start = ((batchIndex[activeTab] ?? 0) * 5) % Math.max(1, list.length);
      return list.slice(start, start + 5);
    }
    {
      const list = options.filter((o) => o.type === 'food');
      const start = ((batchIndex[activeTab] ?? 0) * 5) % Math.max(1, list.length);
      return list.slice(start, start + 5);
    }
  }, [activeTab, options, batchIndex]);

  const visibleSocialFood = useMemo(() => {
    if (activeTab !== 'food') return [];
    const take = (platform: string) => {
      const list = socialFood.filter((item) => item.platform === platform);
      const start = (socialBatch * 2) % Math.max(1, list.length);
      return list.slice(start, start + 2);
    };
    return [...take('抖音'), ...take('小红书')];
  }, [activeTab, socialFood, socialBatch]);

  const travelPlan = useMemo(
    () =>
      options
        .filter((item) => planItemIds.includes(item.id))
        .sort(
          (a, b) =>
            new Date(a.scheduleAt).getTime() - new Date(b.scheduleAt).getTime(),
        ),
    [planItemIds, options],
  );

  const totalBudget = useMemo(
    () => travelPlan.reduce((sum, item) => sum + getOptionPrice(item), 0),
    [travelPlan],
  );

  function addAssistantMessage(content: string) {
    setMessages((prev) => [...prev, { id: buildId(), role: 'assistant', content }]);
  }

  function addTripConfirmMessage(data: Record<string, unknown>) {
    setMessages((prev) => [
      ...prev,
      {
        id: buildId(),
        role: 'assistant',
        content: buildTripConfirmContent(data),
        kind: 'trip-confirm',
        tripConfirmData: data,
      },
    ]);
  }

  function resizeComposer(element: HTMLTextAreaElement) {
    const minHeight = 36;
    const maxHeight = 108;
    element.style.height = `${minHeight}px`;
    const nextHeight = Math.min(Math.max(element.scrollHeight, minHeight), maxHeight);
    element.style.height = `${nextHeight}px`;
    element.style.overflowY = element.scrollHeight > maxHeight ? 'auto' : 'hidden';
  }

  function clearComposer() {
    setDraft('');
    if (typeof window === 'undefined') return;
    window.requestAnimationFrame(() => {
      if (!composerRef.current) return;
      composerRef.current.style.height = '36px';
      composerRef.current.style.overflowY = 'hidden';
    });
  }

  function updateLastAssistantMessage(content: string) {
    setMessages((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.role !== 'assistant') return prev;
      return [...prev.slice(0, -1), { ...last, content }];
    });
  }

  function applySearchData(searchData: Record<string, unknown>) {
    lastSearchRef.current = { ...lastSearchRef.current, ...searchData };
    setBatchIndex({});
    setSocialBatch(0);
    setWeatherData(
      (searchData.weather as { days?: Array<Record<string, unknown>> } | undefined) ?? null,
    );
    const flights = mapFlights(searchData.flights);
    const trains = mapTrains(searchData.trains);
    const hotels = mapHotels(searchData.hotels);
    const spots = mapSpots(searchData.poi);
    const events = mapEvents(searchData.events);
    const food = mapFood(searchData.food_preview ?? searchData.food);
    setSocialFood(Array.isArray(searchData.social_food) ? searchData.social_food : []);
    setOptions([...flights, ...trains, ...hotels, ...spots, ...events, ...food]);
    if (flights.length + trains.length === 0) {
      if (hotels.length > 0) setActiveTab('hotel');
      else if (spots.length > 0) setActiveTab('spot');
      else if (events.length > 0) setActiveTab('event');
      else if (food.length > 0) setActiveTab('food');
    }
  }

  function publishPlanSnapshot(raw: Record<string, unknown>, pending: boolean): RoutePlan | null {
    const plan = (raw.plan ?? raw) as Record<string, unknown>;
    if (!Array.isArray(plan.blocks) || plan.error || !Number.isSafeInteger(plan.revision) || Number(plan.revision) < 1) return null;
    const plans = Array.isArray(plan.plans) ? plan.plans as Array<{style:string;summary?:string}> : [];
    const styles = plans.map(p => p.style).filter(Boolean);
    const snapshot: RoutePlan = {
      revision: typeof plan.revision === 'number' ? plan.revision : undefined,
      audit: pending ? null : currentAudit(raw.audit ?? plan.audit, plan.revision), review_pending: pending,
      history: Array.isArray(raw.history) ? raw.history as Array<Record<string, unknown>> : [],
      review_context: raw.review_context as ReviewContext | undefined ?? routePlan?.review_context,
      basic: plan.basic as Record<string,unknown> | undefined ?? routePlan?.basic,
      destination: String(plan.destination ?? routePlan?.destination ?? ''),
      start_date: String(plan.start_date ?? routePlan?.start_date ?? ''),
      end_date: String(plan.end_date ?? routePlan?.end_date ?? ''),
      styles: styles.length ? styles : routePlan?.styles ?? [],
      summaries: plans.length ? Object.fromEntries(plans.map(p => [p.style, p.summary ?? ''])) : routePlan?.summaries ?? {},
      blocks: plan.blocks as RouteBlock[], legs: Array.isArray(plan.legs) ? plan.legs as RouteLeg[] : [],
      total_cost: plan.total_cost as number | undefined, budget_status: plan.budget_status as string | undefined,
      cost_by_style: plan.cost_by_style as RoutePlan['cost_by_style'], budget_by_style: plan.budget_by_style as RoutePlan['budget_by_style'],
      unpriced_items: plan.unpriced_items as RoutePlan['unpriced_items'], suggestions: plan.suggestions as PlanSuggestions | undefined,
    };
    setRoutePlan(snapshot);
    // Open the generated itinerary immediately; the old cover/selection screen is no longer needed.
    setExpandedStyle(previous => previous && snapshot.styles.includes(previous)
      ? previous : snapshot.styles[0] ?? null);
    setConfirmedStyle(null);
    setSaveState('idle');
    if (raw.search && typeof raw.search === 'object') {
      committedSearchRef.current = raw.search as Record<string,unknown>;
      applySearchData(committedSearchRef.current);
    }
    return snapshot;
  }

  function receiveWorkflowEvent(data: Record<string, unknown>) {
    setReviewProgress(previous => advanceReviewProgress(previous, data));
    if (data.plan) {
      const snapshot = publishPlanSnapshot(data, data.stage === 'reviewing');
      if (snapshot) setActiveStyle(previous => snapshot.styles.includes(previous) ? previous : snapshot.styles[0] ?? '');
    }
    else if (data.search) applySearchData(data.search as Record<string,unknown>);
  }

  function workflowFailed(error: string) {
    setReviewProgress(previous => advanceReviewProgress(previous, {stage:'error',error}));
    setRoutePlan(previous => previous ? {...previous, review_pending:false, audit:{
      schema_version:1,plan_revision:previous.revision!,status:'error',passed:false,error,
      issues:previous.audit?.issues.filter(issue => issue.source === 'rule') ?? [],
      rule_summary:previous.audit?.rule_summary,
    }} : previous);
  }

  async function repairCurrentSnapshot(snapshot: RoutePlan, search: Record<string, unknown>, version: number): Promise<PlanAudit | null> {
    let finalAudit: PlanAudit | null = null;
    setRoutePlan({...snapshot, audit:null, review_pending:true});
    setReviewProgress(advanceReviewProgress(null, {stage:'reviewing',repair_count:0,plan:snapshot}));
    await startPlanStream({plan:snapshot,search}, event => {
      if (version !== planRequestVersionRef.current) return;
      if (event.type === 'node') receiveWorkflowEvent(event.data);
      else if (event.type === 'error') workflowFailed(event.error);
      else {
        const raw = event.data;
        if (typeof raw.error === 'string') {workflowFailed(raw.error); return;}
        const current = publishPlanSnapshot(raw, false);
        finalAudit = current?.audit ?? null;
        if (!finalAudit) { workflowFailed('当前版本没有有效审核结论，请重试。'); return; }
        const workflow = raw.workflow as Record<string,unknown> | undefined;
        setReviewProgress(previous => advanceReviewProgress(previous, {stage:finalAudit?.status ?? 'error',
          plan:current,audit:finalAudit,...workflow,repair_count:workflow?.repair_count}));
      }
    }, '/plan/repair/stream');
    return finalAudit;
  }

  async function runPlan(payload: Record<string, unknown>) {
    const version = ++planRequestVersionRef.current;
    setGeneration({id:version, destination:String(payload.destination ?? ''), progress:5, status:'running'});
    setPlanning(true);
    setReviewProgress(advanceReviewProgress(null, {stage:'planning',repair_count:0}));
    setRoutePlan(previous => previous ? {...previous,audit:null,review_pending:true} : previous);
    setSelectedBlocks(new Set());
    await startPlanStream(payload, event => {
      if (version !== planRequestVersionRef.current) return;
      if (event.type === 'node') {
        const data = event.data;
        const target = data.node === 'search' ? 35 : data.stage === 'reviewing' ? 65 + Number(data.repair_count ?? 0) * 10
          : data.stage === 'repairing' ? 70 + Number(data.repair_count ?? 0) * 10 : 5;
        setGeneration(previous => previous ? {...previous, progress:Math.max(previous.progress, Math.min(95, target))} : previous);
        receiveWorkflowEvent(data); return;
      }
      if (event.type === 'error') {
        workflowFailed(event.error);
        updateLastAssistantMessage('本次处理未完成，现有行程已保留，请查看审核窗口。');
        return;
      }
      const raw = event.data;
      const plan = (raw.plan ?? {}) as Record<string,unknown>;
      if (typeof raw.error === 'string' || typeof plan.error === 'string') {
        workflowFailed(String(raw.error ?? plan.error));
        updateLastAssistantMessage('本次处理未完成，请查看审核窗口后重试。');
        return;
      }
      const snapshot = publishPlanSnapshot(raw,false);
      if (!snapshot || !snapshot.audit) {workflowFailed('未收到完整行程和有效审核结论，请重试。'); return;}
      setGeneration(previous => previous ? {...previous, progress:100, status:'complete'} : previous);
      const workflow = raw.workflow as Record<string,unknown> | undefined;
      setReviewProgress(previous => advanceReviewProgress(previous, {stage:snapshot.audit?.status ?? 'error',plan:snapshot,
        audit:snapshot.audit,...workflow,repair_count:workflow?.repair_count}));
      setActiveStyle(snapshot.styles[0] ?? '');
      setActiveDay('all');
      setPlanRating(null);
      setPlanFeedback('');
      setPlanItemIds([]);
      setDetailItem(null);
      const located = snapshot.blocks.filter(b => b.lng != null).length;
      updateLastAssistantMessage(`已为「${snapshot.destination}」生成 ${snapshot.styles.length} 个方案，地图展示了 ${located} 个地点和 ${snapshot.legs.length} 段路线。\n${reviewMessage(snapshot.audit ?? null)}`);
    }).finally(() => {
      if (version === planRequestVersionRef.current) setPlanning(false);
    });
  }

  async function mutatePlan(change: Record<string, unknown>, successMessage: string): Promise<boolean> {
    if (!routePlan || planning || (requestControllerRef.current?.mutation && !requestControllerRef.current.signal.aborted)) return false;
    const controller: AbortController & { mutation?: boolean } = new AbortController();
    controller.mutation = true;
    const version = ++planRequestVersionRef.current;
    requestControllerRef.current = controller;
    setRoutePlan((prev) => prev ? { ...prev, review_pending: true } : prev);
    setPlanning(true);
    setReviewProgress(advanceReviewProgress(null, {stage:'planning'}));
    try {
      const userId = localStorage.getItem('currentUser') || '';
      let profile: unknown = null;
      let basic: unknown = routePlan.basic ?? null;
      try {
        profile = JSON.parse(localStorage.getItem('userProfile') || 'null');
      } catch {
        profile = null;
      }
      try {
        if (!basic) basic = JSON.parse(localStorage.getItem('tripInfo') || 'null');
      } catch {
        basic = routePlan.basic ?? null;
      }
      profile = profile ?? routePlan.review_context?.profile;
      const raw = await api.plan({
        destination: routePlan.destination,
        start_date: routePlan.start_date,
        end_date: routePlan.end_date,
        ...(userId ? { user_id: userId } : {}),
        ...(profile ? { profile } : {}),
        preferences: routePlan.review_context?.preferences,
        recent_trips: routePlan.review_context?.recent_trips,
        ...(basic ? { basic } : {}),
        plan: routePlan,
        search: committedSearchRef.current,
        modify: {
          blocks: routePlan.blocks,
          plan_style: expandedStyle || activeStyle || routePlan.styles[0],
          ...change,
        },
      }, controller.signal);
      if (controller.signal.aborted || requestControllerRef.current !== controller || version !== planRequestVersionRef.current) return false;
      if (typeof raw.error === 'string') {
        throw new Error(raw.error);
      }
      if (!Array.isArray(raw.blocks)) throw new Error('没有收到更新后的计划，请重试。');
      let audit = currentAudit(raw.audit, raw.revision);
      const editedSnapshot = publishPlanSnapshot(raw, false);
      if (!editedSnapshot) throw new Error('没有收到完整的新行程，请重试。');
      setReviewProgress(advanceReviewProgress(null, {stage:audit?.status ?? 'error',plan:editedSnapshot,audit}));
      const updatedSearch = raw.search && typeof raw.search === 'object' ? raw.search as Record<string, unknown> : committedSearchRef.current;
      committedSearchRef.current = { ...updatedSearch,
        ...(Array.isArray(raw.food) ? { food: raw.food } : {}),
        ...(Array.isArray(raw.food_by_anchor) ? { food_by_anchor: raw.food_by_anchor } : {}),
      };
      applySearchData(committedSearchRef.current);
      if (raw.basic && typeof raw.basic === 'object') {
        localStorage.setItem('tripInfo', JSON.stringify(raw.basic));
        setTripData({ ...raw.basic as Record<string, unknown>,
          destination: raw.destination ?? routePlan.destination,
          start_date: raw.start_date ?? routePlan.start_date,
          end_date: raw.end_date ?? routePlan.end_date,
        });
      }
      setSelectedBlocks(new Set());
      setConfirmedStyle(null);
      setSaveState('idle');
      if (canAutoRepair(audit)) {
        audit = await repairCurrentSnapshot(editedSnapshot, committedSearchRef.current, version);
        if (controller.signal.aborted || version !== planRequestVersionRef.current) return false;
      }
      updateLastAssistantMessage([successMessage, reviewMessage(audit)].join('\n'));
      return true;
    } catch (error) {
      if ((error as Error).name === 'AbortError' || controller.signal.aborted || requestControllerRef.current !== controller || version !== planRequestVersionRef.current) return false;
      workflowFailed(error instanceof Error ? error.message : String(error));
      updateLastAssistantMessage(
        `修改失败：${error instanceof Error ? error.message : String(error)}`,
      );
      return false;
    } finally {
      if (requestControllerRef.current === controller && version === planRequestVersionRef.current) {
        setRoutePlan((prev) => prev ? { ...prev, review_pending: false } : prev);
        requestControllerRef.current = null;
        setPlanning(false);
      }
    }
  }

  async function modifyPlan(instruction: string, mode?: string, targets?: unknown[]) {
    const ids = Array.from(selectedBlocks);
    await mutatePlan({
      action: 'modify', instruction,
      mode: ids.length ? 'block' : mode || 'global',
      ...(ids.length ? { block_ids: ids } : {}),
      ...(targets ? { targets } : {}),
    }, '已按你的需求更新计划，餐饮和地图路线也已同步。');
  }

  async function startTripPlan(data: Record<string, unknown>) {
    const basic = { ...data };
    delete basic.destination;
    delete basic.start_date;
    delete basic.end_date;
    localStorage.setItem('tripInfo', JSON.stringify(basic));
    const userId = localStorage.getItem('currentUser') || '';
    let profile: unknown = null;
    try {
      profile = JSON.parse(localStorage.getItem('userProfile') || 'null');
    } catch {
      profile = null;
    }
    addAssistantMessage('正在搜索并规划行程…');
    await runPlan({
      destination: data.destination,
      start_date: data.start_date,
      end_date: data.end_date,
      ...(userId ? { user_id: userId } : {}),
      ...(profile ? { profile } : {}),
      basic,
    });
  }

  async function confirmTrip() {
    if (!tripConfirm || planning || question) return;
    const data = tripConfirm;
    setTripConfirm(null);
    setCollectingTrip(false);
    setQuestion(null);
    await startTripPlan(data);
  }

  async function resolveIntent(history: ChatMessage[], content: string) {
    const requestVersion = ++intentVersionRef.current;
    setPlanning(true);
    try {
      setTripConfirm(null);
      setPendingConfirm(null);
      if (routePlan && !collectingTrip && selectedBlocks.size > 0) {
        addAssistantMessage('正在按你的需求调整选中的计划项…');
        await modifyPlan(content, 'block');
        return;
      }
      const controller = new AbortController();
      requestControllerRef.current = controller;
      const result = await api.question({
        messages: history.map((m) => ({ role: m.role, content: m.content })),
        has_plan: !!routePlan && !collectingTrip,
        trip_data: tripData,
      }, controller.signal);
      if (requestVersion !== intentVersionRef.current) return;
      if (result.error) throw new Error(String(result.error));
      const action = String(result.action ?? '');
      if (action === 'explain') {
        const answer = String(result.answer ?? '');
        const links = Array.isArray(result.links)
          ? (result.links as Array<{ title?: string; url?: string }>)
          : [];
        const linkText = links
          .map((link) => `${link.title ?? ''}：${link.url ?? ''}`)
          .join('\n');
        addAssistantMessage(
          `${answer}${linkText ? `\n\n相关链接：\n${linkText}` : ''}`,
        );
        return;
      }
      if (action === 'ask') {
        setTripData((result.data ?? {}) as Record<string, unknown>);
        setCollectingTrip(true);
        const questions = Array.isArray(result.questions) ? result.questions as TripQuestion[] : [];
        setQuestion({
          question: String(result.question ?? ''),
          questions: questions.length ? questions : [{ field: 'trip_details', question: String(result.question ?? '请补充旅行信息'), options: [] }],
        });
        addAssistantMessage(String(result.question ?? ''));
        return;
      }
      if (action === 'confirm_trip') {
        const data = (result.data ?? {}) as Record<string, unknown>;
        setTripData(data);
        setCollectingTrip(true);
        setQuestion(null);
        setTripConfirm(data);
        addTripConfirmMessage(data);
        return;
      }
      if (action === 'plan') {
        const data = (result.data ?? {}) as Record<string, unknown>;
        setTripData(data);
        setCollectingTrip(false);
        setQuestion(null);
        setTripConfirm(null);
        await startTripPlan(data);
        return;
      }
      if (action === 'confirm') {
        setPendingConfirm({
          summary: String(result.summary ?? ''),
          mode: String(result.mode ?? 'global'),
          targets: Array.isArray(result.targets)
            ? (result.targets as unknown[])
            : undefined,
          instruction: String(result.instruction ?? content),
        });
        addAssistantMessage(
          `${String(result.summary ?? '')}\n是否按此修改？`,
        );
        return;
      }
      if (action === 'modify') {
        setQuestion(null);
        addAssistantMessage('正在修改计划…');
        const selectedBlocksNow = selectedBlocks.size > 0 && routePlan
          ? routePlan.blocks.filter((block) => selectedBlocks.has(block.id))
          : [];
        const mode = selectedBlocksNow.length > 0
          ? 'block'
          : String(result.mode ?? '');
        const targets = selectedBlocksNow.length > 0
          ? selectedBlocksNow.map((block) => ({
              name: block.name,
              type: block.type,
              day: block.day,
            }))
          : Array.isArray(result.targets)
            ? (result.targets as unknown[])
            : undefined;
        await modifyPlan(
          String(result.instruction ?? content),
          mode,
          targets,
        );
        return;
      }
      addAssistantMessage('暂时没能理解这条需求，请用文字描述目的地、时间或想调整的内容。');
    } catch (error) {
      if ((error as Error).name === 'AbortError' || requestVersion !== intentVersionRef.current) return;
      addAssistantMessage(
        `处理失败：${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      if (requestVersion === intentVersionRef.current) setPlanning(false);
    }
  }

  async function submitQuestion(reply: string) {
    if (!question || planning) return;
    const value = reply.trim();
    if (!value) return;
    const history: ChatMessage[] = [
      ...messages,
      { id: buildId(), role: 'user', content: value },
    ];
    setMessages((prev) => [
      ...prev,
      { id: buildId(), role: 'user', content: value },
    ]);
    setQuestion(null);
    setTripConfirm(null);
    await resolveIntent(history, value);
  }

  async function confirmModification() {
    if (!pendingConfirm) return;
    const confirm = pendingConfirm;
    setPendingConfirm(null);
    addAssistantMessage('正在修改计划…');
    await modifyPlan(confirm.instruction, confirm.mode, confirm.targets);
  }



  function handleSend() {
    const content = draft.trim();
    if (!content || planning) return;

    if (pendingAdd) {
      setMessages((prev) => [
        ...prev,
        { id: buildId(), role: 'user', content },
      ]);
      clearComposer();
      if (/不加|取消|算了|不要/.test(content)) {
        setPendingAdd(null);
        addAssistantMessage('好的，已取消添加。');
        return;
      }
      handleAddReply(content);
      return;
    }

    setMessages((prev) => [
      ...prev,
      { id: buildId(), role: 'user', content },
    ]);
    clearComposer();

    const history: ChatMessage[] = [
      ...messages,
      { id: buildId(), role: 'user', content },
    ];
    setQuestion(null);
    setTripConfirm(null);
    setPendingConfirm(null);
    void resolveIntent(history, content);
  }

  function stopPlanning() {
    intentVersionRef.current += 1;
    planRequestVersionRef.current += 1;
    setRoutePlan((prev) => prev ? { ...prev, review_pending: false } : prev);
    requestControllerRef.current?.abort();
    stopPlanStream();
    setPlanning(false);
    setReviewProgress(previous => advanceReviewProgress(previous, {stage:'cancelled'}));
    addAssistantMessage('已停止本次处理。你可以继续补充或调整需求。');
  }

  function handleComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      handleSend();
    }
  }

  function addToPlan(item: OptionItem) {
    if (planning) return;
    if (routePlan) {
      startAdd(item);
      return;
    }
    setPlanItemIds((current) => current.includes(item.id) ? current : [...current, item.id]);
  }

  async function removeFromPlan(itemId: string) {
    if (planning) return;
    const item = options.find((option) => option.id === itemId);
    if (routePlan && item) {
      const style = expandedStyle || activeStyle;
      const matches = routePlan.blocks.filter((block) => block.name === item.title && (!style || block.plan_style === style));
      if (matches.length) {
        addAssistantMessage(`正在从计划移除「${item.title}」…`);
        await mutatePlan({ action: 'delete', block_ids: matches.map((block) => block.id) }, `已移除「${item.title}」，行程与路线已更新。`);
      }
    }
    setPlanItemIds((current) => current.filter((id) => id !== itemId));
  }

  function togglePlan(item: OptionItem) {
    if (planning) return;
    if (isInPlan(item)) void removeFromPlan(item.id);
    else addToPlan(item);
  }

  function isInPlan(item: OptionItem) {
    if (routePlan) {
      const style = expandedStyle || activeStyle;
      return routePlan.blocks.some((block) => block.name === item.title && (!style || block.plan_style === style));
    }
    return planItemIds.includes(item.id);
  }

  function optionToBlockType(item: OptionItem) {
    return { flight: '交通', hotel: '酒店', spot: '景点', event: '活动', food: '美食' }[item.type];
  }

  function startAdd(item: OptionItem) {
    if (!routePlan || planning) return;
    setPendingAdd(item);
    const day = item.suggestedDay || (activeDay !== 'all' ? activeDay : 1);
    setAddDay(Math.min(getPlanDayCount(routePlan), Math.max(1, day)));
    setAddTime('');
    addAssistantMessage(`把「${item.title}」安排在哪一天？可在下面选择，也可以回复“第2天 14:00”。`);
  }

  async function handleAddReply(content: string) {
    if (!pendingAdd || !routePlan || planning) return;
    const match = content.match(/第?\s*(\d+|[一二三四五六七八九十]+)\s*天/);
    const names: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
    const day = match ? names[match[1]] ?? Number(match[1]) : addDay;
    const maxDay = getPlanDayCount(routePlan);
    if (!Number.isFinite(day) || day < 1 || day > maxDay) {
      addAssistantMessage(`当前行程是${maxDay}天，请选择第1到第${maxDay}天。`);
      return;
    }
    const time = content.match(/(\d{1,2}:\d{2})/)?.[1] || addTime;
    const item: Record<string, unknown> = {
      name: pendingAdd.title, type: optionToBlockType(pendingAdd), note: pendingAdd.description,
      link: pendingAdd.url || '', lng: pendingAdd.lng, lat: pendingAdd.lat,
      ...(pendingAdd.priceKnown !== false ? { price: getOptionPrice(pendingAdd) } : {}),
      source_option_id: pendingAdd.id,
      ...(pendingAdd.type === 'food' ? {
        rating: pendingAdd.rating, cuisine: pendingAdd.cuisine,
        walking_distance_m: pendingAdd.walkingDistanceM,
        walking_duration_s: pendingAdd.walkingDurationS,
        walking_origin: pendingAdd.walkingOrigin,
      } : {}),
      ...(time ? { time } : {}),
    };
    const candidate = pendingAdd;
    addAssistantMessage(`正在把「${candidate.title}」安排到第${day}天…`);
    const success = await mutatePlan({ action: 'add', day, item, block_ids: Array.from(selectedBlocks) },
      `已把「${candidate.title}」安排到第${day}天，计划与地图已同步。`);
    if (success) setPendingAdd(null);
  }

  function toggleBlock(blockId: string) {
    if (planning) return;
    setSelectedBlocks((prev) => {
      const next = new Set(prev);
      if (next.has(blockId)) {
        next.delete(blockId);
      } else {
        next.add(blockId);
      }
      return next;
    });
  }

  async function toggleFoodOption(block: RouteBlock, option: NonNullable<RouteBlock['options']>[number]) {
    if (planning || block.selected_option === option.name) return;
    addAssistantMessage(`正在将${block.meal || '这餐'}换成「${option.name}」…`);
    const walking = formatWalkingInfo(option.walking_distance_m, option.walking_duration_s).replace(' / ', '/');
    const straight = option.distance_m != null ? `距${block.anchor_name || '相邻景点'}约${option.distance_m}米（直线距离）` : '';
    const note = straight ? `${block.meal || '用餐'} · ${straight}${walking ? `；${walking}` : ''}` : block.note;
    await mutatePlan({
      action: 'update', block_ids: [block.id],
      item: { ...option, type: '美食', selected_option: option.name, options: block.options,
        note },
    }, `已选用「${option.name}」，餐费和地图路线已同步。`);
  }

  function chooseStyle(style: string) {
    setActiveStyle(style);
    setExpandedStyle(style);
    setActiveDay('all');
    setSelectedBlocks(new Set());
  }

  function nextBatch(tab: OptionType) {
    setBatchIndex((prev) => ({
      ...prev,
      [tab]: (prev[tab] ?? 0) + 1,
    }));
  }

  function nextSocialBatch() {
    setSocialBatch((prev) => prev + 1);
  }

  function confirmPlan() {
    if (!expandedStyle || !routePlan || confirmDisabledReason) return;
    setPlanRating(null);
    setPlanFeedback('');
    setSaveState('idle');
    setConfirmModalOpen(true);
  }

  function closeConfirmModal() {
    setConfirmModalOpen(false);
  }

  async function acceptPlan() {
    if (!expandedStyle || !routePlan || confirmDisabledReason || saveState === 'saving') return;
    const userId = localStorage.getItem('currentUser') || '';
    if (!userId) {
      setSaveState('error');
      setConfirmModalOpen(false);
      return;
    }
    const savedRequestVersion = planRequestVersionRef.current;
    setSaveState('saving');
    try {
      const payload: Parameters<typeof api.saveTripMemory>[0] = {
        user_id: userId,
        destination: routePlan.destination,
        start_date: routePlan.start_date,
        end_date: routePlan.end_date,
        chosen_plan_style: expandedStyle,
        final_plan: routePlan,
        conversation: messages,
      };
      if (planRating != null) payload.rating = planRating;
      if (planFeedback.trim()) payload.feedback = planFeedback.trim();
      await api.saveTripMemory(payload);
      if (savedRequestVersion !== planRequestVersionRef.current) return;
      setConfirmedStyle(expandedStyle);
      setSaveState('saved');
      setConfirmModalOpen(false);
      void api.reportBehavior(userId, 'confirm', expandedStyle, 'plan');
      if (planRating != null) {
        void api.reportBehavior(userId, 'rate', expandedStyle, String(planRating));
      }
    } catch {
      if (savedRequestVersion === planRequestVersionRef.current) setSaveState('error');
    }
  }

  function handleMapDragStart(event: ReactPointerEvent<HTMLDivElement>) {
    const card = event.currentTarget.parentElement;
    if (!card) return;

    event.currentTarget.setPointerCapture(event.pointerId);
    const rect = card.getBoundingClientRect();
    mapDragRef.current = {
      offsetX: event.clientX - rect.left,
      offsetY: event.clientY - rect.top,
    };
  }

  function handleMapDrag(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = mapDragRef.current;
    const card = event.currentTarget.parentElement;
    if (!drag || !card || typeof window === 'undefined') return;

    const rect = card.getBoundingClientRect();
    const minX = 12;
    const minY = 68;
    const maxX = Math.max(minX, window.innerWidth - rect.width - 12);
    const maxY = Math.max(minY, window.innerHeight - rect.height - 12);

    setMapPosition({
      x: Math.min(maxX, Math.max(minX, event.clientX - drag.offsetX)),
      y: Math.min(maxY, Math.max(minY, event.clientY - drag.offsetY)),
    });
  }

  function handleMapDragEnd(event: ReactPointerEvent<HTMLDivElement>) {
    mapDragRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function toggleMapExpanded() {
    const nextExpanded = !isMapExpanded;
    setIsMapExpanded(nextExpanded);

    if (typeof window === 'undefined') return;

    const width = nextExpanded
      ? Math.min(900, window.innerWidth - 80)
      : Math.min(360, window.innerWidth - 32);
    const height = nextExpanded
      ? Math.min(650, window.innerHeight - 92)
      : Math.min(318, window.innerHeight - 96);

    setMapPosition({
      x: Math.max(12, Math.round((window.innerWidth - width) / 2)),
      y: Math.max(68, Math.round((window.innerHeight - height) / 2)),
    });
  }

  function renderOptionMeta(item: OptionItem) {
    if (item.type === 'flight') {
      return (
        <>
          <span>{item.mode === 'train' ? '高铁' : '航班'} {item.scheduleLabel}</span>
          <span>{item.duration}</span>
          <span>{item.priceKnown === false ? '暂无报价' : formatPrice(item.price)}</span>
        </>
      );
    }

    if (item.type === 'hotel') {
      return (
        <>
          <span>{item.scheduleLabel}</span>
          <span>{item.rating} ★</span>
          <span>{item.priceKnown === false ? '暂无报价' : formatPrice(item.totalPrice)}</span>
        </>
      );
    }

    if (item.type === 'event') {
      const hasSchedule = Boolean(item.scheduleLabel);
      const hasPrice = item.price > 0;

      if (!hasSchedule && !hasPrice) return null;

      return (
        <>
          {hasSchedule && <span>{item.scheduleLabel}</span>}
          {hasPrice && <span>{formatPrice(item.price)}</span>}
        </>
      );
    }

    if (item.type === 'food') {
      return (
        <>
          {item.scheduleLabel && <span>{item.scheduleLabel}</span>}
          {item.walkingDistanceM != null && <span>{formatWalkingInfo(item.walkingDistanceM, item.walkingDurationS)}</span>}
          {item.distanceM != null && <span>距相邻景点约{Math.round(item.distanceM)}米（直线）</span>}
          <span>{item.cuisine}</span>
          <span>{item.rating > 0 ? `${item.rating} ★` : item.businessArea}</span>
          <span>{item.pricePerPerson > 0 ? `${formatPrice(item.pricePerPerson)}/人` : '美食'}</span>
        </>
      );
    }

    return (
      <>
        <span>{item.scheduleLabel}</span>
        <span>{item.recommendedDuration}</span>
        <span>{item.priceKnown === false ? '暂无报价' : formatPrice(item.ticketPrice)}</span>
      </>
    );
  }

  const expandedKnownCost = expandedStyle && routePlan
    ? routePlan.cost_by_style?.[expandedStyle] ?? routePlan.blocks
        .filter((block) => block.plan_style === expandedStyle)
        .reduce((sum, block) => sum + (block.price ?? 0), 0)
    : 0;
  const expandedUnpricedCount = expandedStyle && routePlan
    ? routePlan.unpriced_items?.[expandedStyle]?.length ?? 0
    : 0;
  const expandedBudgetStatus = expandedStyle && routePlan
    ? routePlan.budget_by_style?.[expandedStyle]
      ?? (expandedStyle === routePlan.styles[0] ? routePlan.budget_status : undefined)
      ?? 'unknown'
    : 'unknown';
  const expandedBudgetStatusLabel = expandedBudgetStatus === 'over'
    ? '超出预算'
    : expandedBudgetStatus === 'ok'
      ? '预算内'
      : '待确认';

  return (
    <section className="travel-agent-workbench">
      <div className="travel-agent-layout">
        {/* 左侧：AI 对话 */}
        <aside className="ta-chat-panel">
          <div className="ta-panel-header">
            <div>
              <span className="ta-section-kicker">AI ASSISTANT</span>
              <h2>对话界面</h2>
            </div>
          </div>

          <div className="ta-chat-messages">
            {messages.map((message) => {
              const confirmFields = message.tripConfirmData
                ? getTripConfirmFields(message.tripConfirmData)
                : [];
              const isActiveConfirm = Boolean(
                tripConfirm && message.tripConfirmData === tripConfirm,
              );

              return (
                <div
                  key={message.id}
                  className={`ta-message ${message.role === 'user' ? 'user' : 'assistant'}`}
                >
                  {message.role === 'assistant' && (
                    <span className="ta-message-avatar">TR</span>
                  )}

                  {message.kind === 'trip-confirm' && message.tripConfirmData ? (
                    <div className="ta-trip-confirm-card">
                      <div className="ta-trip-confirm-heading">
                        <strong>请确认本次旅行信息</strong>
                      </div>

                      <div className="ta-trip-confirm-grid">
                        {confirmFields.map((field) => (
                          <div
                            key={field.label}
                            className={`ta-trip-confirm-item${field.wide ? ' wide' : ''}`}
                          >
                            <span>{field.label}</span>
                            <strong>{field.value}</strong>
                          </div>
                        ))}
                      </div>

                      <p className="ta-trip-confirm-note">
                        请核对以上信息；如需调整，可以继续补充。
                      </p>

                      {isActiveConfirm && (
                        <div className="ta-trip-confirm-actions">
                          <button
                            type="button"
                            className="ta-clarify-submit"
                            onClick={() => void confirmTrip()}
                            disabled={planning}
                          >
                            确认并开始规划
                          </button>
                          <button
                            type="button"
                            className="ta-clarify-option"
                            onClick={() => setTripConfirm(null)}
                          >
                            继续修改
                          </button>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="ta-message-bubble">{message.content}</div>
                  )}
                </div>
              );
            })}
            {routePlan?.suggestions && (() => {
              const cards = [
                ...routePlan.suggestions.spots_rank.map((card) => ({ ...card, kind: '景点' })),
                ...routePlan.suggestions.spots_match.map((card) => ({ ...card, kind: '景点' })),
                ...routePlan.suggestions.restaurants.map((card) => ({ ...card, kind: '餐厅' })),
                ...routePlan.suggestions.hotels.map((card) => ({ ...card, kind: '酒店' })),
              ];
              return cards.length > 0 ? (
                <div className="ta-message assistant">
                  <span className="ta-message-avatar">TR</span>
                  <div className="ta-message-bubble ta-suggestion-bubble">
                    <div className="ta-suggestion-scroll">
                      {cards.map((card) => (
                        <a
                          key={`${card.kind}-${card.name}`}
                          className="ta-suggestion-card"
                          href={card.link || undefined}
                          target={card.link ? '_blank' : undefined}
                          rel="noreferrer"
                        >
                          <span className="ta-suggestion-kind">{card.kind}</span>
                          {card.image && (
                            <img
                              className="ta-suggestion-card-img"
                              src={card.image}
                              alt={card.name}
                            />
                          )}
                          <div className="ta-suggestion-card-body">
                            <strong>{card.name}</strong>
                            <span>{card.subtitle}</span>
                          </div>
                        </a>
                      ))}
                    </div>
                  </div>
                </div>
              ) : null;
            })()}
            {generation && <CityGuide key={generation.id} destination={generation.destination}
              progress={generation.progress} status={generation.status} />}
          </div>

          {question && (
            <TripQuestions key={JSON.stringify(question.questions)} questions={question.questions} busy={planning}
              onSubmit={(reply) => void submitQuestion(reply)} />
          )}

          {pendingAdd && routePlan && (
            <div className="ta-clarify-card">
              <div className="ta-clarify-title">安排「{pendingAdd.title}」</div>
              <div className="ta-clarify-row ta-add-schedule-row">
                <label>日期
                  <select value={addDay} onChange={(event) => setAddDay(Number(event.target.value))} disabled={planning}>
                    {Array.from({ length: getPlanDayCount(routePlan) }, (_, index) => index + 1)
                      .map((day) => <option key={day} value={day}>第{day}天</option>)}
                  </select>
                </label>
                <label>时间（可选）
                  <input type="time" value={addTime} onChange={(event) => setAddTime(event.target.value)} disabled={planning} />
                </label>
              </div>
              <p className="ta-question-hint">不填时间时，系统会安排空档；餐厅和酒店优先替换当天已有推荐。</p>
              <button type="button" className="ta-clarify-submit" disabled={planning}
                onClick={() => void handleAddReply(`第${addDay}天 ${addTime}`)}>加入当天计划</button>
              <button type="button" className="ta-clarify-option" disabled={planning} onClick={() => setPendingAdd(null)}>取消</button>
            </div>
          )}

          {pendingConfirm && (
            <div className="ta-clarify-card">
              <div className="ta-clarify-title">修改确认</div>
              <p className="ta-confirm-summary">{pendingConfirm.summary}</p>
              <div className="ta-clarify-row">
                <button
                  type="button"
                  className="ta-clarify-submit"
                  onClick={() => void confirmModification()}
                  disabled={planning}
                >
                  确认修改
                </button>
                <button
                  type="button"
                  className="ta-clarify-option"
                  onClick={() => setPendingConfirm(null)}
                >
                  取消
                </button>
              </div>
            </div>
          )}

          <div className="ta-chat-composer">
            {selectedBlocks.size > 0 && (
              <div className="ta-selection-hint">
                已选{selectedBlocks.size}项，输入需求将调整这些项目。
                <button type="button" disabled={planning} onClick={() => setSelectedBlocks(new Set())}>取消选择</button>
              </div>
            )}
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(event) => {
                setDraft(event.target.value);
                resizeComposer(event.currentTarget);
              }}
              onKeyDown={handleComposerKeyDown}
              placeholder="描述你的旅行想法，或继续补充信息…"
              rows={1}
            />
            {planning ? (
              <button
                type="button"
                className="ta-stop-button"
                onClick={stopPlanning}
              >
                停止
              </button>
            ) : (
              <button
                type="button"
                className="ta-send-button"
                onClick={handleSend}
                disabled={!draft.trim()}
              >
                发送
              </button>
            )}
          </div>
        </aside>

        {/* 中间：旅行计划 */}
        <section className="ta-plan-card ta-plan-column">
          {expandedStyle && routePlan ? (
            <div className="ta-plan-detail">
              <div className="ta-plan-detail-toolbar">
                <span className="ta-plan-detail-toolbar-label">旅行计划</span>
                {routePlan.styles.length > 1 && (
                  <div className="ta-plan-style-switch" role="group" aria-label="切换旅行方案">
                    {routePlan.styles.map(style => (
                      <button
                        key={style}
                        type="button"
                        className={expandedStyle === style ? 'active' : ''}
                        aria-pressed={expandedStyle === style}
                        onClick={() => chooseStyle(style)}
                        disabled={planning}
                      >
                        {style}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="ta-plan-detail-title">
                <strong>{expandedStyle}</strong>
                <p>{routePlan.summaries[expandedStyle] ?? ''}</p>
              </div>
              <section className="ta-budget-summary" aria-label="预算摘要">
                <div className="ta-budget-summary-heading">预算摘要</div>
                <div className="ta-budget-summary-grid">
                  <div className="ta-budget-summary-item primary">
                    <span>已知费用</span>
                    <strong>¥ {expandedKnownCost.toLocaleString()}</strong>
                  </div>
                  <div className="ta-budget-summary-item">
                    <span>未报价</span>
                    <strong>{expandedUnpricedCount} 项</strong>
                  </div>
                  <div className={`ta-budget-summary-item status ${expandedBudgetStatus}`}>
                    <span>预算状态</span>
                    <strong>{expandedBudgetStatusLabel}</strong>
                  </div>
                </div>
                {expandedBudgetStatus === 'over' ? (
                  <p className="ta-budget-summary-note warning">已知费用已超出总预算。</p>
                ) : expandedBudgetStatus === 'unknown' || expandedUnpricedCount > 0 ? (
                  <p className="ta-budget-summary-note">部分项目价格尚未确认，当前合计仅包含已知金额。</p>
                ) : (
                  <p className="ta-budget-summary-note">当前已知费用处于预算范围内。</p>
                )}
              </section>
              {confirmedStyle === expandedStyle && selectedBlocks.size === 0 && (
                <div className="ta-plan-confirmed">已确认该计划</div>
              )}
              <div className="ta-plan-detail-scroll">
                {routePlan.blocks
                  .filter((b) => b.plan_style === expandedStyle)
                  .reduce((acc: Array<{ day: number; blocks: RouteBlock[] }>, b) => {
                    const last = acc[acc.length - 1];
                    if (last && Number(last.day) === Number(b.day)) {
                      last.blocks.push(b);
                    } else {
                      acc.push({ day: Number(b.day), blocks: [b] });
                    }
                    return acc;
                  }, [])
                  .map(({ day, blocks: dayBlocks }) => {
                    const dayDate = dayBlocks[0]?.date ?? '';
                    const weather = (weatherData?.days ?? []).find((d) => d.date === dayDate);
                    return (
                      <div key={day} className="ta-plan-style-day" tabIndex={-1}
                        ref={element => { if (element) dayRefs.current.set(day, element); else dayRefs.current.delete(day); }}>
                        <div className="ta-plan-style-day-label">Day {day}</div>
                        {weather && (
                          <div className="ta-plan-block ta-plan-block-weather">
                            <div className="ta-plan-block-left">
                              <span>天气</span>
                              <span>当日</span>
                            </div>
                            <div className="ta-plan-block-body">
                              <strong>
                                {String(weather.weather ?? '')} {String(weather.temp_min ?? '')}~{String(weather.temp_max ?? '')}°C
                              </strong>
                              <p>湿度 {String(weather.humidity ?? '')}%</p>
                            </div>
                          </div>
                        )}
                        {dayBlocks.map((block, index) => {
                          const nextBlock = dayBlocks[index + 1];
                          const leg = nextBlock ? expandedLegs.find((l) => l.from === block.id && l.to === nextBlock.id) : undefined;
                          const legSummary = formatLegSummary(leg);
                          return (
                            <div key={block.id}>
                              <div
                                ref={element => { if (element) activityRefs.current.set(block.id, element); else activityRefs.current.delete(block.id); }}
                                className={`ta-plan-block ta-plan-block-${block.type}${
                                  selectedBlocks.has(block.id) ? ' selected' : ''
                                }${reviewLocation?.style === expandedStyle && reviewLocation.ids.includes(block.id) ? ' ta-review-highlight' : ''}`}
                            onClick={(event) => {
                              if (!(event.target as Element).closest('button, a, input')) toggleBlock(block.id);
                            }}
                            onKeyDown={(event) => {
                              if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) {
                                event.preventDefault();
                                toggleBlock(block.id);
                              }
                            }}
                            role="button"
                            tabIndex={0}
                            aria-pressed={selectedBlocks.has(block.id)}
                            aria-label={`选择计划项 ${block.name}`}
                          >
                            <div className="ta-plan-block-left">
                              <span>{block.time}</span>
                              <span>{block.meal || block.type}</span>
                              <button type="button" className="ta-plan-select-button" disabled={planning}
                                aria-pressed={selectedBlocks.has(block.id)} aria-label={`选择${block.name}进行修改`}
                                onClick={() => toggleBlock(block.id)}>{selectedBlocks.has(block.id) ? '已选中' : '选择修改'}</button>
                            </div>
                            <div className="ta-plan-block-body">
                              {block.link ? (
                                <a
                                  className="ta-plan-block-link"
                                  href={block.link}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  {block.name}
                                </a>
                              ) : (
                                <strong>{block.name}</strong>
                              )}
                              {block.note && <p>{block.note}</p>}
                              {block.type === '景点' && block.match_score != null && (
                                <span className="ta-plan-block-match">匹配度 {block.match_score}</span>
                              )}
                              {block.price != null && (
                                <span className="ta-plan-block-price">
                                  {block.price_known === false ? '暂无报价' : `¥ ${block.price.toLocaleString()}`}
                                </span>
                              )}
                              {block.options && block.options.length > 0 && (
                                <div className="ta-plan-block-options">
                                  {block.options.map((option) => {
                                    const active = block.selected_option === option.name || block.name === option.name;
                                    return (
                                      <span className="ta-food-option-wrap" key={option.name}>
                                        <button
                                          type="button"
                                          className={`ta-food-option${active ? ' active' : ''}`}
                                          disabled={planning}
                                          onClick={() => void toggleFoodOption(block, option)}
                                        >
                                          {option.name}
                                          {option.price != null && option.price > 0
                                            ? ` · ¥${option.price}`
                                            : ''}
                                          {formatWalkingInfo(option.walking_distance_m, option.walking_duration_s)
                                            ? ` · ${formatWalkingInfo(option.walking_distance_m, option.walking_duration_s)}`
                                            : ''}
                                          {option.distance_m != null
                                            ? ` · 直线约${Math.round(option.distance_m)}米`
                                            : option.distance_km != null ? ` · 直线约${option.distance_km}km` : ''}
                                          {option.rating != null ? ` · ${option.rating}分` : ''}
                                        </button>
                                        {option.link && (
                                          <a
                                            className="ta-food-option-link"
                                            href={option.link}
                                            target="_blank"
                                            rel="noreferrer"
                                            aria-label={`打开${option.name}地图链接`}
                                          >
                                            查看地图 ↗
                                          </a>
                                        )}
                                      </span>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                              </div>
                              {legSummary ? (
                                <div className="ta-plan-block-leg">{legSummary}</div>
                              ) : null}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })}
              </div>
              {confirmedStyle === expandedStyle ? (
                <div className="ta-plan-rating-status">
                  {saveState === 'saved' && <span>已保存</span>}
                  {saveState === 'error' && <span>保存失败，请确认已登录</span>}
                </div>
              ) : (
                <button
                  type="button"
                  className="ta-plan-confirm-button"
                  disabled={!!confirmDisabledReason}
                  title={confirmDisabledReason || undefined}
                  onClick={confirmPlan}
                >
                  确认计划
                </button>
              )}
              {confirmDisabledReason && <p className="ta-review-confirm-hint">{confirmDisabledReason}</p>}
            </div>
          ) : (
            <>
          <div className="ta-panel-header ta-compact-header">
            <div>
              <span className="ta-section-kicker">TRAVEL PLAN</span>
              <h2>旅行计划</h2>
            </div>

            {travelPlan.length > 0 && (
              <div className="ta-plan-budget">
                <span>预计</span>
                <strong>{formatPrice(totalBudget)}</strong>
              </div>
            )}
          </div>

          {travelPlan.length > 0 && !routePlan ? (
            <div className="ta-plan-timeline">
              {travelPlan.map((item) => (
                <div
                  className="ta-plan-row"
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => setDetailItem(item)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault();
                      setDetailItem(item);
                    }
                  }}
                  aria-label={`查看 ${item.title} 的详细信息`}
                >
                  <div className={`ta-plan-type ${item.type}`}>
                    {getOptionIcon(item)}
                  </div>

                  <div className="ta-plan-time">{item.scheduleLabel}</div>

                  <div className="ta-plan-content">
                    <strong>{item.title}</strong>
                    <span>
                      {getOptionTypeLabel(item.type)} · {item.location}
                    </span>
                  </div>

                  <button
                    type="button"
                    className="ta-plan-remove"
                    disabled={planning}
                    onClick={(event) => {
                      event.stopPropagation();
                      removeFromPlan(item.id);
                    }}
                    aria-label={`从旅行计划移除 ${item.title}`}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="ta-plan-empty">
              {routePlan && routePlan.styles.length > 0 ? (
                <div className="ta-plan-loading">正在整理方案详情…</div>
              ) : (
                <>
                  <div>先在左侧描述旅行需求，生成后可把右侧候选加入行程。</div>
                  <p className="ta-plan-helper">
                    例如「从上海去杭州玩3天，2人，预算5000元」
                  </p>
                </>
              )}
            </div>
          )}
            </>
          )}
        </section>

        {/* 右侧：可供选择的行程 */}
        <section className="ta-options-panel ta-options-column">
          <div className="ta-options-header">
            <div>
              <span className="ta-section-kicker">OPTIONS</span>
              <h2>待选行程</h2>
            </div>

            <div className="ta-tabs">
              <button
                type="button"
                className={activeTab === 'flight' ? 'active' : ''}
                onClick={() => setActiveTab('flight')}
              >
                出行
              </button>
              <button
                type="button"
                className={activeTab === 'hotel' ? 'active' : ''}
                onClick={() => setActiveTab('hotel')}
              >
                酒店
              </button>
              <button
                type="button"
                className={activeTab === 'spot' ? 'active' : ''}
                onClick={() => setActiveTab('spot')}
              >
                景点
              </button>
              <button
                type="button"
                className={activeTab === 'event' ? 'active' : ''}
                onClick={() => setActiveTab('event')}
              >
                活动
              </button>
              <button
                type="button"
                className={activeTab === 'food' ? 'active' : ''}
                onClick={() => setActiveTab('food')}
              >
                美食
              </button>
            </div>

            {(activeTab === 'hotel' || activeTab === 'spot' || activeTab === 'event' || activeTab === 'food') && (
              <button
                type="button"
                className="ta-batch-button"
                onClick={() => {
                  nextBatch(activeTab);
                  if (activeTab === 'food') nextSocialBatch();
                }}
              >
                换一批
              </button>
            )}
          </div>

          <div className="ta-option-list">
            {activeTab === 'food' && visibleSocialFood.length > 0 && (
              <div className="ta-social-food">
                <div className="ta-social-food-title">抖音 / 小红书笔记</div>
                {visibleSocialFood.map((item, index) => (
                  <a
                    key={`${item.platform}-${index}`}
                    className="ta-social-food-item"
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <span className="ta-social-food-platform">{item.platform}</span>
                    <span className="ta-social-food-text">{item.title}</span>
                  </a>
                ))}
              </div>
            )}

            {visibleOptions.map((item) => {
              const added = isInPlan(item);

              return (
                <article
                  key={item.id}
                  className={`ta-option-card ta-option-card-${item.type} ${added ? 'selected' : ''}`}
                  onClick={() => setDetailItem(item)}
                >
                  <div className="ta-option-main">
                    {item.image && (
                      <img
                        className="ta-option-image"
                        src={item.image}
                        alt={item.title}
                        loading="lazy"
                      />
                    )}
                    <div className="ta-option-title-row">
                      <div>
                        {item.type === 'flight' && (
                          <span className="ta-carrier-badge">{getCarrierBadge(item)}</span>
                        )}
                        <h3>{item.title}</h3>
                        <p>{item.subtitle}</p>
                      </div>

                      {added && (
                        <span className="ta-selected-badge">已加入计划</span>
                      )}
                    </div>

                    <div className="ta-option-meta">
                      {renderOptionMeta(item)}
                    </div>

                    <div className="ta-option-tags">
                      {item.tags.map((tag) => (
                        <span key={tag}>{tag}</span>
                      ))}
                    </div>
                  </div>

                  <div
                    className="ta-option-actions"
                    onClick={(event) => event.stopPropagation()}
                  >
                    {item.url && (
                      <a
                        className="ta-ghost-button"
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        相关链接
                      </a>
                    )}
                    <button
                      className="ta-ghost-button"
                      type="button"
                      onClick={() => setDetailItem(item)}
                    >
                      查看详情
                    </button>

                    <button
                      className={added ? 'ta-remove-button' : 'ta-primary-button'}
                      type="button"
                      disabled={planning}
                      onClick={() => startAdd(item)}
                    >
                      添加到计划
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      </div>

      {/* 悬浮地图：拖动标题栏可以自由移动 */}
      <PlanReviewWindow
        mapDock={isMapExpanded ? getMapDockPosition() : mapPosition}
        plan={routePlan ?? {blocks:[],review_pending:planning}} busy={planning} progress={reviewProgress}
        onLocate={locateReviewIssue} onRetry={() => void retryReview()} onRepair={() => void retryReview()} />

      {isMapHidden && (
        <button
          type="button"
          className="ta-map-side-tab"
          onClick={() => {
            setMapPosition(getMapDockPosition());
            setIsMapExpanded(false);
            setIsMapHidden(false);
          }}
          aria-label="显示地图"
        >
          <span className="ta-map-side-arrow">◀</span>
          <span className="ta-map-side-label">地图</span>
        </button>
      )}

      {!isMapHidden && (
      <section
        className={`ta-floating-map ${isMapExpanded ? 'expanded' : ''}`}
        style={{ left: mapPosition.x, top: mapPosition.y }}
        aria-label="可移动地图"
      >
        <div
          className="ta-floating-map-handle"
          onPointerDown={handleMapDragStart}
          onPointerMove={handleMapDrag}
          onPointerUp={handleMapDragEnd}
          onPointerCancel={handleMapDragEnd}
        >
          <div>
            <span className="ta-section-kicker">MAP</span>
            <strong>{routePlan?.destination ? `${routePlan.destination} 路线` : '地图'}</strong>
          </div>
          <div className="ta-floating-map-actions">
            <span className="ta-map-drag-hint">拖动移动</span>
            <button
              type="button"
              className="ta-map-hide-button"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                setIsMapHidden(true);
              }}
              aria-label="隐藏地图"
            >
              隐藏
            </button>
            <button
              type="button"
              className="ta-map-expand-button"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                toggleMapExpanded();
              }}
              aria-label={isMapExpanded ? '缩小地图' : '展开地图'}
            >
              {isMapExpanded ? '缩小' : '展开'}
            </button>
          </div>
        </div>

        {routePlan && routePlan.styles.length > 1 && (
          <div className="ta-floating-map-style-tabs ta-tabs">
            {routePlan.styles.map((style) => (
              <button
                key={style}
                type="button"
                className={style === activeStyle ? 'active' : ''}
                disabled={planning}
                onClick={() => chooseStyle(style)}
              >
                {style}
              </button>
            ))}
          </div>
        )}

        {routePlan ? (
          <>
            <div className="ta-tabs ta-map-days ta-floating-map-days">
              <button
                type="button"
                className={activeDay === 'all' ? 'active' : ''}
                onClick={() => setActiveDay('all')}
              >
                全部
              </button>
              {planDays.map((day) => (
                <button
                  key={day}
                  type="button"
                  className={activeDay === day ? 'active' : ''}
                  onClick={() => setActiveDay(day)}
                >
                  D{day}
                </button>
              ))}
            </div>
            <TripMap
              blocks={styleBlocks}
              legs={styleLegs}
              day={activeDay}
            />
          </>
        ) : (
          <div className="ta-map-placeholder ta-floating-map-placeholder">
            <span>{planning ? '正在规划路线…' : '地图区域'}</span>
            {planning && <p>生成后会在这里显示每天的路线</p>}
          </div>
        )}
      </section>
      )}

      {detailItem && (
        <div className="ta-modal-backdrop" onClick={() => setDetailItem(null)}>
          <div
            className="ta-detail-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="ta-detail-header">
              <div>
                <span className="ta-section-kicker">
                  {getOptionTypeLabel(detailItem.type)}
                </span>
                <h3>{detailItem.title}</h3>
                <p>
                  {detailItem.subtitle} · {detailItem.scheduleLabel}
                </p>
              </div>

              <button
                type="button"
                className="ta-close-button"
                onClick={() => setDetailItem(null)}
              >
                ×
              </button>
            </div>

            <div className="ta-detail-body">
              {detailItem.type === 'flight' && (
                <>
                  <div className="ta-detail-grid">
                    <div>
                      <span>出发</span>
                      <strong>{detailItem.from}</strong>
                    </div>
                    <div>
                      <span>到达</span>
                      <strong>{detailItem.to}</strong>
                    </div>
                    <div>
                      <span>出发时间</span>
                      <strong>{detailItem.departTime}</strong>
                    </div>
                    <div>
                      <span>到达时间</span>
                      <strong>{detailItem.arriveTime}</strong>
                    </div>
                    <div>
                      <span>{detailItem.mode === 'train' ? '车次' : '航司'}</span>
                      <strong>{detailItem.carrier}{detailItem.code}</strong>
                    </div>
                    <div>
                      <span>时长</span>
                      <strong>{detailItem.duration}</strong>
                    </div>
                  </div>

                  <div className="ta-detail-foot">
                    <span>位置：{detailItem.location}</span>
                    <strong>{detailItem.priceKnown === false ? '暂无报价' : formatPrice(detailItem.price)}</strong>
                  </div>
                </>
              )}

              {detailItem.type === 'hotel' && (
                <>
                  <div className="ta-detail-grid">
                    <div>
                      <span>区域</span>
                      <strong>{detailItem.district}</strong>
                    </div>
                    <div>
                      <span>房型</span>
                      <strong>{detailItem.roomType}</strong>
                    </div>
                    <div>
                      <span>入住</span>
                      <strong>{detailItem.checkIn}</strong>
                    </div>
                    <div>
                      <span>离店</span>
                      <strong>{detailItem.checkOut}</strong>
                    </div>
                    <div>
                      <span>评分</span>
                      <strong>{detailItem.rating} ★</strong>
                    </div>
                    <div>
                      <span>每晚均价</span>
                      <strong>{detailItem.priceKnown === false ? '暂无报价' : formatPrice(detailItem.nightlyPrice)}</strong>
                    </div>
                  </div>

                  <div className="ta-detail-foot">
                    <span>位置：{detailItem.location}</span>
                    <strong>{detailItem.priceKnown === false ? '暂无报价' : formatPrice(detailItem.totalPrice)}</strong>
                  </div>
                </>
              )}

              {detailItem.type === 'spot' && (
                <>
                  <div className="ta-detail-grid">
                    <div>
                      <span>区域</span>
                      <strong>{detailItem.area}</strong>
                    </div>
                    <div>
                      <span>开放时间</span>
                      <strong>{detailItem.openHours}</strong>
                    </div>
                    <div>
                      <span>建议时长</span>
                      <strong>{detailItem.recommendedDuration}</strong>
                    </div>
                    <div>
                      <span>门票</span>
                      <strong>{detailItem.priceKnown === false ? '暂无报价' : formatPrice(detailItem.ticketPrice)}</strong>
                    </div>
                  </div>

                  <div className="ta-detail-foot">
                    <span>位置：{detailItem.location}</span>
                    <strong>{detailItem.subtitle}</strong>
                  </div>
                </>
              )}

              {detailItem.type === 'food' && (
                <>
                  <div className="ta-detail-grid">
                    <div>
                      <span>菜系</span>
                      <strong>{detailItem.cuisine || '—'}</strong>
                    </div>
                    <div>
                      <span>评分</span>
                      <strong>{detailItem.rating > 0 ? `${detailItem.rating} ★` : '—'}</strong>
                    </div>
                    <div>
                      <span>人均</span>
                      <strong>{detailItem.pricePerPerson > 0 ? formatPrice(detailItem.pricePerPerson) : '—'}</strong>
                    </div>
                    <div>
                      <span>商圈</span>
                      <strong>{detailItem.businessArea || '—'}</strong>
                    </div>
                    {detailItem.walkingDistanceM != null && (
                      <div>
                        <span>相邻景点步行路线</span>
                        <strong>{formatWalkingInfo(detailItem.walkingDistanceM, detailItem.walkingDurationS)}</strong>
                      </div>
                    )}
                    {detailItem.distanceM != null && (
                      <div>
                        <span>距相邻景点（直线）</span>
                        <strong>约{Math.round(detailItem.distanceM)}米</strong>
                      </div>
                    )}
                  </div>

                  <div className="ta-detail-foot">
                    <span>地址：{detailItem.address || detailItem.location || '—'}</span>
                    <strong>
                      {detailItem.detailUrl && (
                        <a href={detailItem.detailUrl} target="_blank" rel="noreferrer">
                          查看详情
                        </a>
                      )}
                      {detailItem.mapUrl && (
                        <a href={detailItem.mapUrl} target="_blank" rel="noreferrer">
                          地图
                        </a>
                      )}
                    </strong>
                  </div>
                </>
              )}

              <div className="ta-detail-description">
                <span>说明</span>
                <p>{detailItem.description}</p>
              </div>

              <div className="ta-modal-actions">
                <button
                  type="button"
                  className="ta-ghost-button"
                  onClick={() => setDetailItem(null)}
                >
                  关闭
                </button>

                <button
                  type="button"
                  className={
                    isInPlan(detailItem)
                      ? 'ta-remove-button'
                      : 'ta-primary-button'
                  }
                  disabled={planning}
                  onClick={() => togglePlan(detailItem)}
                >
                  {isInPlan(detailItem) ? '从计划移除' : '添加到计划'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {confirmModalOpen && (
        <div className="ta-modal-backdrop" onClick={closeConfirmModal}>
          <div
            className="ta-detail-modal ta-confirm-plan-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="ta-detail-header">
              <div>
                <span className="ta-section-kicker">CONFIRM PLAN</span>
                <h3>确认接受该计划？</h3>
                <p>请对本次计划打分并提出建议，确认后即保存为历史行程。</p>
              </div>
              <button
                type="button"
                className="ta-close-button"
                onClick={closeConfirmModal}
              >
                ×
              </button>
            </div>

            <div className="ta-confirm-plan-body">
              <div className="ta-confirm-plan-section">
                <span className="ta-confirm-plan-label">打分</span>
                <div className="ta-plan-rating-buttons">
                  {[
                    { label: '很满意', value: 5 },
                    { label: '满意', value: 4 },
                    { label: '一般', value: 3 },
                    { label: '不满意', value: 1 },
                  ].map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      className={`ta-plan-rating-button${
                        planRating === option.value ? ' active' : ''
                      }`}
                      disabled={saveState === 'saving' || !!confirmDisabledReason}
                      onClick={() => setPlanRating(option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="ta-confirm-plan-section">
                <span className="ta-confirm-plan-label">建议</span>
                <textarea
                  className="ta-plan-rating-feedback"
                  value={planFeedback}
                  onChange={(event) => setPlanFeedback(event.target.value)}
                  placeholder="对本次计划有什么建议？（可选）"
                  rows={3}
                />
              </div>

              {saveState === 'error' && (
                <div className="ta-plan-rating-status">保存失败，请确认已登录</div>
              )}
            </div>

            <div className="ta-modal-actions">
              <button
                type="button"
                className="ta-ghost-button"
                onClick={closeConfirmModal}
              >
                取消
              </button>
              <button
                type="button"
                className="ta-primary-button"
                disabled={saveState === 'saving' || !!confirmDisabledReason}
                onClick={() => void acceptPlan()}
              >
                {saveState === 'saving' ? '保存中…' : '确认接受'}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export default AgentPage;
