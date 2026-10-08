import { useEffect, useRef } from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';

export type RouteBlock = {
  id: string;
  plan_style?: string;
  day: number;
  date?: string;
  type: string;
  time: string;
  name: string;
  note?: string;
  match_score?: number;
  link?: string;
  price?: number | null;
  unit_price?: number | null;
  price_basis?: 'group' | 'per_person';
  activity_window?: { start_min: number; end_min: number };
  locked?: boolean;
  price_known?: boolean;
  meal?: string;
  anchor_name?: string;
  selected_option?: string;
  source_option_id?: string;
  distance_m?: number;
  walking_distance_m?: number | null;
  walking_duration_s?: number | null;
  walking_origin?: string | null;
  options?: Array<{
    name: string;
    price?: number | null;
    link?: string;
    lng?: number;
    lat?: number;
    rating?: number;
    cuisine?: string;
    business_area?: string;
    distance_km?: number | null;
    distance_m?: number;
    walking_distance_m?: number | null;
    walking_duration_s?: number | null;
    walking_origin?: string | null;
  }>;
  lng?: number;
  lat?: number;
};

export type RouteLeg = {
  plan_style?: string;
  day: number;
  from: string;
  to: string;
  mode: 'walk' | 'transit' | 'drive';
  distance_m: number;
  duration_s: number;
  lines?: string[];
  polyline: [number, number][];
};

type TripMapProps = {
  blocks: RouteBlock[];
  legs: RouteLeg[];
  day: number | 'all';
};

// 高德瓦片与后端返回的坐标同为 GCJ-02，直接叠加不会偏移
const AMAP_TILES =
  'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';

const DAY_COLORS = ['#3b6cf6', '#f0802b', '#18a672', '#c2419b', '#7a5af5', '#d6a312'];
const MODE_LABELS: Record<RouteLeg['mode'], string> = {
  walk: '步行',
  transit: '公交/地铁',
  drive: '打车',
};

const dayColor = (day: number) => DAY_COLORS[(Math.max(day, 1) - 1) % DAY_COLORS.length];

function describeLeg(leg: RouteLeg) {
  const minutes = Math.max(1, Math.round(leg.duration_s / 60));
  const km = (leg.distance_m / 1000).toFixed(1);
  const lines = leg.lines?.length ? ` · ${leg.lines.join(' → ')}` : '';
  return `${MODE_LABELS[leg.mode]} · ${minutes}分钟 · ${km}km${lines}`;
}

function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function TripMap({ blocks, legs, day }: TripMapProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<L.LayerGroup | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const map = L.map(containerRef.current, { zoomControl: true, attributionControl: false });
    L.tileLayer(AMAP_TILES, { subdomains: '1234', maxZoom: 18 }).addTo(map);
    map.setView([30.27, 120.15], 11);
    mapRef.current = map;
    layerRef.current = L.layerGroup().addTo(map);

    // 右侧面板高度随布局变化，需要通知 Leaflet 重新计算尺寸
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    const layer = layerRef.current;
    if (!map || !layer) return;
    layer.clearLayers();

    const byId = new Map(blocks.map((b) => [b.id, b]));
    const located = blocks.filter(
      (b) => b.lng != null && b.lat != null && (day === 'all' || b.day === day),
    );
    const bounds = L.latLngBounds([]);

    const legKey = (from: string, to: string) => `${from}>${to}`;
    const legByPair = new Map(legs.map((leg) => [legKey(leg.from, leg.to), leg]));
    const visibleLegs = legs.filter((leg) => day === 'all' || leg.day === day);

    for (const leg of visibleLegs) {
      const from = byId.get(leg.from);
      const to = byId.get(leg.to);
      // 局部修改后坐标变了但路线还没重算：不画旧路线，交给下面的直线兜底
      const stale =
        !from || !to || from.lng == null || to.lng == null ||
        Math.abs(from.lng - leg.polyline[0]?.[0]) > 0.01 ||
        Math.abs((to.lng ?? 0) - leg.polyline[leg.polyline.length - 1]?.[0]) > 0.01;
      if (stale || leg.polyline.length < 2) {
        legByPair.delete(legKey(leg.from, leg.to));
        continue;
      }
      const latlngs = leg.polyline.map(([lng, lat]) => L.latLng(lat, lng));
      L.polyline(latlngs, {
        color: dayColor(leg.day),
        weight: leg.mode === 'drive' ? 5 : 4,
        opacity: 0.85,
        dashArray: leg.mode === 'walk' ? '6 8' : undefined,
      })
        .bindTooltip(`${escapeHtml(from!.name)} → ${escapeHtml(to!.name)}<br>${describeLeg(leg)}`, {
          sticky: true,
        })
        .addTo(layer);
      latlngs.forEach((p) => bounds.extend(p));
    }

    // 没有真实路线的相邻地点用灰色虚线相连，至少能看出先后顺序
    const byDay = new Map<number, RouteBlock[]>();
    for (const b of located) byDay.set(b.day, [...(byDay.get(b.day) ?? []), b]);
    for (const stops of byDay.values()) {
      stops.forEach((b, index) => {
        const next = stops[index + 1];
        if (!next || legByPair.has(legKey(b.id, next.id))) return;
        L.polyline(
          [L.latLng(b.lat!, b.lng!), L.latLng(next.lat!, next.lng!)],
          { color: '#9aa2b0', weight: 2, dashArray: '4 6' },
        ).addTo(layer);
      });
      stops.forEach((b, index) => {
        const isHotel = b.type === '酒店';
        const icon = L.divIcon({
          className: 'ta-map-marker',
          html: `<span style="background:${dayColor(b.day)}">${isHotel ? 'H' : index + 1}</span>`,
          iconSize: [24, 24],
          iconAnchor: [12, 12],
        });
        const point = L.latLng(b.lat!, b.lng!);
        L.marker(point, { icon })
          .bindTooltip(`D${b.day} · ${escapeHtml(b.time)}<br><strong>${escapeHtml(b.name)}</strong>`)
          .addTo(layer);
        bounds.extend(point);
      });
    }

    if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 15 });
  }, [blocks, legs, day]);

  return <div ref={containerRef} className="ta-map-canvas" />;
}

export default TripMap;
