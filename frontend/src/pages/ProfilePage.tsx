import { useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { UserProfile } from '../api/types';

const EMPTY_PROFILE: UserProfile = {
  age_group: '',
  gender: '',
  identity: '',
  city: '',
  travel_style: [],
};

const AGE_GROUPS = ['18 岁以下', '18-25', '26-35', '36-45', '46-60', '60 岁以上'];
const GENDERS = ['男', '女', '不便透露'];
const IDENTITIES = ['学生', '上班族', '自由职业', '创业者', '退休', '其他'];
const TRAVEL_STYLES = [
  '自然景观', '历史人文', '主题娱乐', '城市地标与购物', '户外运动与体验',
];

function ProfileField({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="profile-field">
      <div className="profile-field-label">{label}</div>
      {children}
    </div>
  );
}

export function ProfilePage() {
  const navigate = useNavigate();
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
  const [error, setError] = useState('');

  const completed = [form.age_group, form.gender, form.identity, form.city.trim(),
    form.travel_style.length ? 'yes' : ''].filter(Boolean).length;

  function setSingle(key: 'age_group' | 'gender' | 'identity' | 'city', value: string) {
    setSaved(false);
    setError('');
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function toggleStyle(value: string) {
    setSaved(false);
    setError('');
    setForm((prev) => ({
      ...prev,
      travel_style: prev.travel_style.includes(value)
        ? prev.travel_style.filter((v) => v !== value)
        : [...prev.travel_style, value],
    }));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    localStorage.setItem('userProfile', JSON.stringify(form));
    setSaved(false);
    setError('');

    const userId = localStorage.getItem('currentUser');
    if (!userId) {
      navigate('/agent');
      return;
    }
    try {
      await api.saveProfile(userId, form);
      setSaved(true);
      navigate('/agent');
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="profile-page">
      <header className="profile-header">
        <div className="profile-intro">
          <span className="profile-eyebrow">个人设置</span>
          <h1>旅行偏好</h1>
          <p>记录你的出行习惯，让后续行程推荐更贴近你的喜好。信息可以随时修改。</p>
        </div>
        <div className="profile-completion">
          <span>资料完成度</span>
          <div className="profile-completion-value"><strong>{completed}</strong><span> / 5 项</span></div>
          <div className="profile-progress-track" role="progressbar"
            aria-label="旅行偏好资料完成度" aria-valuemin={0} aria-valuemax={5} aria-valuenow={completed}>
            <span style={{ width: `${completed * 20}%` }} />
          </div>
        </div>
      </header>

      <form className="profile-form" onSubmit={handleSubmit}>
        <section className="profile-section" aria-labelledby="profile-basic-title">
          <div className="profile-section-heading">
            <h2 id="profile-basic-title">基本信息</h2>
            <p>这些信息有助于提供符合出行习惯的建议</p>
          </div>

          <div className="profile-field-grid">
            <ProfileField label="年龄范围">
              <div className="profile-options">
                {AGE_GROUPS.map(o => (
                  <button key={o} type="button" aria-pressed={form.age_group === o}
                    className={`profile-option${form.age_group === o ? ' active' : ''}`}
                    onClick={() => setSingle('age_group', o)}>{o}</button>
                ))}
              </div>
            </ProfileField>

            <ProfileField label="性别">
              <div className="profile-options">
                {GENDERS.map(o => (
                  <button key={o} type="button" aria-pressed={form.gender === o}
                    className={`profile-option${form.gender === o ? ' active' : ''}`}
                    onClick={() => setSingle('gender', o)}>{o}</button>
                ))}
              </div>
            </ProfileField>

            <ProfileField label="当前身份">
              <div className="profile-options">
                {IDENTITIES.map(o => (
                  <button key={o} type="button" aria-pressed={form.identity === o}
                    className={`profile-option${form.identity === o ? ' active' : ''}`}
                    onClick={() => setSingle('identity', o)}>{o}</button>
                ))}
              </div>
            </ProfileField>

            <div className="profile-field">
              <label className="profile-field-label" htmlFor="profile-city">常驻城市</label>
              <input id="profile-city" className="profile-city-input" value={form.city}
                onChange={(event) => setSingle('city', event.target.value)}
                placeholder="例如：上海" autoComplete="address-level2" />
            </div>
          </div>
        </section>

        <section className="profile-section profile-section-interests" aria-labelledby="profile-interest-title">
          <div className="profile-section-heading">
            <h2 id="profile-interest-title">旅行兴趣</h2>
            <p>可以多选，优先推荐你真正感兴趣的旅行体验</p>
          </div>
          <div className="profile-options profile-options-interests">
            {TRAVEL_STYLES.map(o => (
              <button key={o} type="button" aria-pressed={form.travel_style.includes(o)}
                className={`profile-option${form.travel_style.includes(o) ? ' active' : ''}`}
                onClick={() => toggleStyle(o)}>
                {form.travel_style.includes(o) && <span className="profile-check" aria-hidden="true">✓</span>}
                {o}
              </button>
            ))}
          </div>
        </section>

        <footer className="profile-actions">
          <span className="profile-actions-hint">保存后，之后仍可以回来调整偏好。</span>
          <button type="submit" className="profile-save-button">保存并继续</button>
          {saved && <span className="profile-success" role="status">已保存</span>}
          {error && <span className="login-error" role="alert">{error}</span>}
        </footer>
      </form>
    </div>
  );
}
