import { Link, useParams } from 'react-router-dom';
import type { SavedRouteBlock } from '../api/types';
import { api } from '../api/client';
import { useApi } from '../hooks/useApi';

function formatRecordDate(value: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
  if (!match) return value;
  return `${Number(match[2])}月${Number(match[3])}日`;
}

function groupBlocksByDay(blocks: SavedRouteBlock[]) {
  const groups = new Map<number, SavedRouteBlock[]>();
  for (const block of blocks) {
    const day = Number(block.day) || 1;
    groups.set(day, [...(groups.get(day) ?? []), block]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a - b)
    .map(([day, dayBlocks]) => ({ day, blocks: dayBlocks }));
}

export function TripDetailPage() {
  const { id } = useParams();
  const memoryId = Number(id);
  const userId = localStorage.getItem('currentUser') || '';
  const { data: memory, loading, error } = useApi(
    () => api.getTripMemory(memoryId, userId || undefined),
    [memoryId, userId],
  );

  if (!Number.isFinite(memoryId)) return <p className="error">无效的行程编号。</p>;
  if (loading) return <p>加载中…</p>;
  if (error) return <p className="error">{error}</p>;
  if (!memory) return <p>未找到行程。</p>;

  const plan = memory.final_plan ?? {};
  const chosenStyle = memory.chosen_plan_style ?? plan.styles?.[0] ?? '';
  const allBlocks = Array.isArray(plan.blocks) ? plan.blocks : [];
  const hasStyledBlocks = allBlocks.some((block) => Boolean(block.plan_style));
  const blocks = allBlocks.filter(
    (block) => !hasStyledBlocks || !chosenStyle || block.plan_style === chosenStyle,
  );
  const days = groupBlocksByDay(blocks);
  const summary = chosenStyle ? plan.summaries?.[chosenStyle] ?? '' : '';
  const weatherDays = plan.weather?.days ?? [];

  return (
    <section className="trip-memory-detail-page">
      <Link to="/trips" className="back-link trip-memory-back-link">
        ← 返回历史行程
      </Link>

      <div className="trip-memory-detail-card">
        <div className="trip-memory-detail-heading">
          <div>
            <span className="trip-memory-record-label">旅行记录</span>
            <h1>{memory.destination}</h1>
            <p className="trip-memory-detail-meta">
              {formatRecordDate(memory.start_date)} — {formatRecordDate(memory.end_date)}
              {days.length > 0 ? ` · ${days.length}天` : ''}
            </p>
          </div>
          <span className="trip-memory-detail-confirmed">已确认</span>
        </div>

        <div className="trip-memory-detail-style">
          <span>已确认方案</span>
          <strong>{chosenStyle || '已保存方案'}</strong>
          {summary && <p>{summary}</p>}
        </div>

        {days.length === 0 ? (
          <p className="muted trip-memory-empty-plan">该历史记录暂无具体日程。</p>
        ) : (
          <div className="trip-memory-days">
            {days.map(({ day, blocks: dayBlocks }) => {
              const date = dayBlocks[0]?.date ?? '';
              const weather = weatherDays.find((item) => item.date === date);
              return (
                <section key={day} className="trip-memory-day">
                  <div className="trip-memory-day-title">
                    <strong>第 {day} 天</strong>
                    {date && <span>{formatRecordDate(date)}</span>}
                  </div>

                  {weather && (
                    <div className="trip-memory-itinerary-row trip-memory-weather-row">
                      <div className="trip-memory-itinerary-left">
                        <span>当日</span>
                        <small>天气</small>
                      </div>
                      <div className="trip-memory-itinerary-body">
                        <strong>
                          {String(weather.weather ?? '')}{' '}
                          {String(weather.temp_min ?? '')}~{String(weather.temp_max ?? '')}°C
                        </strong>
                        {weather.humidity != null && (
                          <p>湿度 {String(weather.humidity)}%</p>
                        )}
                      </div>
                    </div>
                  )}

                  {dayBlocks.map((block) => (
                    <div key={block.id} className="trip-memory-itinerary-row">
                      <div className="trip-memory-itinerary-left">
                        <span>{block.time || '—'}</span>
                        <small>{block.type || '行程'}</small>
                      </div>
                      <div className="trip-memory-itinerary-body">
                        <strong>{block.name}</strong>
                        {block.note && <p>{block.note}</p>}
                      </div>
                    </div>
                  ))}
                </section>
              );
            })}
          </div>
        )}

        {(memory.rating != null || memory.feedback) && (
          <div className="trip-memory-review">
            <strong>你的评价</strong>
            {memory.rating != null && <span>{memory.rating} / 5</span>}
            {memory.feedback && <p>{memory.feedback}</p>}
          </div>
        )}
      </div>
    </section>
  );
}
