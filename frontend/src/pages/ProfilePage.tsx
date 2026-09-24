import { useState } from 'react';
import type { FormEvent } from 'react';
import type { UserProfile } from '../api/types';

const EMPTY_PROFILE: UserProfile = {
  age_group: '',
  mbti: '',
  city: '',
  companion: [],
  pace: [],
  budget: [],
  accommodation: [],
  transport: [],
  interests: [],
  dietary: [],
};

const AGE_GROUPS = ['18 岁以下', '18–25', '26–35', '36–50', '50 岁以上'];
const MBTI_TYPES = [
  'INTJ', 'INTP', 'ENTJ', 'ENTP',
  'INFJ', 'INFP', 'ENFJ', 'ENFP',
  'ISTJ', 'ISFJ', 'ESTJ', 'ESFJ',
  'ISTP', 'ISFP', 'ESTP', 'ESFP',
];
const COMPANIONS = ['独自出行', '情侣/伴侣', '家庭（带娃）', '家庭（带老人）', '朋友结伴'];
const PACES = ['慢节奏深度游', '适中', '紧凑打卡'];
const BUDGETS = ['经济实惠', '舒适型', '豪华型', '不设限'];
const ACCOMMODATIONS = ['酒店', '民宿', '客栈'];
const TRANSPORTS = ['高铁', '飞机', '自驾'];
const INTERESTS = ['自然风光', '人文历史', '主题乐园', '博物馆', '美食购物', '户外运动', '温泉度假', '摄影'];
const DIETARY = ['海鲜过敏', '不吃辣', '素食', '清真', '其他'];

type MultiKey =
  | 'companion'
  | 'pace'
  | 'budget'
  | 'accommodation'
  | 'transport'
  | 'interests'
  | 'dietary';

function RadioGroup({
  options,
  value,
  onChange,
}: {
  options: string[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="options">
      {options.map((o) => (
        <label key={o} className="radio">
          <input type="radio" checked={value === o} onChange={() => onChange(o)} />
          {o}
        </label>
      ))}
    </div>
  );
}

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

export function ProfilePage() {
  const [form, setForm] = useState<UserProfile>(() => {
    try {
      const raw = localStorage.getItem('userProfile');
      return raw
        ? { ...EMPTY_PROFILE, ...(JSON.parse(raw) as Partial<UserProfile>) }
        : EMPTY_PROFILE;
    } catch {
      return EMPTY_PROFILE;
    }
  });
  const [saved, setSaved] = useState(false);

  function setSingle(key: 'age_group' | 'mbti' | 'city', value: string) {
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
    localStorage.setItem('userProfile', JSON.stringify(form));
    setSaved(true);
  }

  return (
    <section className="survey">
      <h1>用户画像问卷</h1>
      <p className="muted">花 1 分钟告诉我们你的偏好，用来更精准地推荐行程、酒店和活动。</p>

      <form onSubmit={handleSubmit}>
        <fieldset className="survey-section">
          <legend>基本信息</legend>
          <div className="field">
            <span className="field-label">年龄段</span>
            <RadioGroup
              options={AGE_GROUPS}
              value={form.age_group}
              onChange={(v) => setSingle('age_group', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">MBTI</span>
            <select
              className="field-select"
              value={form.mbti}
              onChange={(e) => setSingle('mbti', e.target.value)}
            >
              <option value="">请选择（可选）</option>
              {MBTI_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <span className="field-label">常住城市</span>
            <input
              value={form.city}
              onChange={(e) => setSingle('city', e.target.value)}
              placeholder="例如：北京"
            />
          </div>
        </fieldset>

        <fieldset className="survey-section">
          <legend>旅行偏好</legend>
          <div className="field">
            <span className="field-label">通常和谁一起旅行（可多选）</span>
            <ChipGroup
              options={COMPANIONS}
              values={form.companion}
              onToggle={(v) => toggleMulti('companion', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">旅行节奏（可多选）</span>
            <ChipGroup options={PACES} values={form.pace} onToggle={(v) => toggleMulti('pace', v)} />
          </div>
          <div className="field">
            <span className="field-label">预算偏好（可多选）</span>
            <ChipGroup
              options={BUDGETS}
              values={form.budget}
              onToggle={(v) => toggleMulti('budget', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">住宿偏好（可多选）</span>
            <ChipGroup
              options={ACCOMMODATIONS}
              values={form.accommodation}
              onToggle={(v) => toggleMulti('accommodation', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">交通偏好（可多选）</span>
            <ChipGroup
              options={TRANSPORTS}
              values={form.transport}
              onToggle={(v) => toggleMulti('transport', v)}
            />
          </div>
        </fieldset>

        <fieldset className="survey-section">
          <legend>兴趣与需求</legend>
          <div className="field">
            <span className="field-label">感兴趣的旅行内容（可多选）</span>
            <ChipGroup
              options={INTERESTS}
              values={form.interests}
              onToggle={(v) => toggleMulti('interests', v)}
            />
          </div>
          <div className="field">
            <span className="field-label">饮食忌口（可多选）</span>
            <ChipGroup
              options={DIETARY}
              values={form.dietary}
              onToggle={(v) => toggleMulti('dietary', v)}
            />
          </div>
        </fieldset>

        <div className="survey-actions">
          <button type="submit">保存画像</button>
          {saved && <span className="saved-hint">已保存到本地 ✓</span>}
        </div>
      </form>

      {saved && <pre className="survey-preview">{JSON.stringify(form, null, 2)}</pre>}
    </section>
  );
}
