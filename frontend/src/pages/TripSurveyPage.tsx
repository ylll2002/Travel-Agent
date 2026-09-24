import { useState } from 'react';
import type { FormEvent } from 'react';
import type { TripInfo } from '../api/types';

const EMPTY_TRIP: TripInfo = {
  destination: '',
  origin: '',
  start_date: '',
  end_date: '',
  travelers: '',
  companions: [],
  budget_tiers: [],
  total_budget: '',
  purposes: [],
  special_needs: [],
};

const TRAVELERS = ['1 人', '2 人', '3 人', '4 人', '5 人', '6 人及以上'];
const COMPANIONS = ['独自', '伴侣', '带孩子', '带老人', '朋友', '同事'];
const BUDGET_TIERS = ['经济', '舒适', '豪华', '不设限'];
const PURPOSES = ['休闲度假', '观光打卡', '美食之旅', '文化历史', '亲子游', '蜜月', '商务出行', '户外探险', '购物'];
const SPECIAL_NEEDS = ['无障碍设施', '带宠物', '签证咨询', '接送机', '行李寄存'];

type MultiKey = 'companions' | 'budget_tiers' | 'purposes' | 'special_needs';
type SingleKey = 'destination' | 'origin' | 'start_date' | 'end_date' | 'travelers' | 'total_budget';

function ChipGroup({
  options,
  values,
  onToggle,
}: {
  options: string[];
  values: string[];
  onToggle: (v: string) => void;
}) {
  return (
    <div className="options">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          className={`chip${values.includes(o) ? ' active' : ''}`}
          onClick={() => onToggle(o)}
        >
          {o}
        </button>
      ))}
    </div>
  );
}

export function TripSurveyPage() {
  const [form, setForm] = useState<TripInfo>(() => {
    try {
      const raw = localStorage.getItem('tripInfo');
      return raw ? { ...EMPTY_TRIP, ...(JSON.parse(raw) as Partial<TripInfo>) } : EMPTY_TRIP;
    } catch {
      return EMPTY_TRIP;
    }
  });
  const [saved, setSaved] = useState(false);

  function setSingle(key: SingleKey, value: string) {
    setSaved(false);
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function toggleMulti(key: MultiKey, value: string) {
    setSaved(false);
    setForm((prev) => {
      const current = prev[key];
      return {
        ...prev,
        [key]: current.includes(value)
          ? current.filter((v) => v !== value)
          : [...current, value],
      };
    });
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    localStorage.setItem('tripInfo', JSON.stringify(form));
    setSaved(true);
  }

  return (
    <section className="survey">
      <h1>本次旅行信息</h1>
      <p className="muted">告诉我们这次出行的基本信息，用来为你规划行程、订酒店和机票。</p>

      <form onSubmit={handleSubmit}>
        <fieldset className="survey-section">
          <legend>行程信息</legend>
          <div className="field">
            <span className="field-label">目的地</span>
            <input
              required
              value={form.destination}
              onChange={(e) => setSingle('destination', e.target.value)}
              placeholder="例如：杭州"
            />
          </div>
          <div className="field">
            <span className="field-label">出发地</span>
            <input
              value={form.origin}
              onChange={(e) => setSingle('origin', e.target.value)}
              placeholder="例如：北京"
            />
          </div>
          <div className="field">
            <span className="field-label">出发日期</span>
            <input
              type="date"
              value={form.start_date}
              onChange={(e) => setSingle('start_date', e.target.value)}
            />
          </div>
          <div className="field">
            <span className="field-label">返程日期</span>
            <input
              type="date"
              value={form.end_date}
              onChange={(e) => setSingle('end_date', e.target.value)}
            />
          </div>
          <div className="field">
            <span className="field-label">出行人数</span>
            <select
              className="field-select"
              value={form.travelers}
              onChange={(e) => setSingle('travelers', e.target.value)}
            >
              <option value="">请选择</option>
              {TRAVELERS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
        </fieldset>

        <fieldset className="survey-section">
          <legend>预算</legend>
          <div className="field">
            <span className="field-label">预算档位（可多选）</span>
            <ChipGroup
              options={BUDGET_TIERS}
              values={form.budget_tiers}
              onToggle={(v) => toggleMulti('budget_tiers', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">总预算金额（可选）</span>
            <input
              type="number"
              min="0"
              value={form.total_budget}
              onChange={(e) => setSingle('total_budget', e.target.value)}
              placeholder="单位：元，例如 5000"
            />
          </div>
        </fieldset>

        <fieldset className="survey-section">
          <legend>出行目的与需求</legend>
          <div className="field">
            <span className="field-label">同行人（可多选）</span>
            <ChipGroup
              options={COMPANIONS}
              values={form.companions}
              onToggle={(v) => toggleMulti('companions', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">旅行目的（可多选）</span>
            <ChipGroup
              options={PURPOSES}
              values={form.purposes}
              onToggle={(v) => toggleMulti('purposes', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">特殊需求（可多选）</span>
            <ChipGroup
              options={SPECIAL_NEEDS}
              values={form.special_needs}
              onToggle={(v) => toggleMulti('special_needs', v)}
            />
          </div>
        </fieldset>

        <div className="survey-actions">
          <button type="submit">保存</button>
          {saved && <span className="saved-hint">已保存到本地 ✓</span>}
        </div>
      </form>

      {saved && <pre className="survey-preview">{JSON.stringify(form, null, 2)}</pre>}
    </section>
  );
}
