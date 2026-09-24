import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';

const PHONE_RE = /^1[3-9]\d{9}$/;

export function LoginPage() {
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [sentCode, setSentCode] = useState('');
  const [countdown, setCountdown] = useState(0);
  const [error, setError] = useState('');
  const navigate = useNavigate();

  useEffect(() => {
    if (countdown <= 0) return;
    const timer = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(timer);
  }, [countdown]);

  function sendCode() {
    if (!PHONE_RE.test(phone)) {
      setError('请输入正确的手机号');
      return;
    }
    setError('');
    // 开发模式：直接生成并展示验证码；真实环境这里应调用后端短信接口
    const c = String(Math.floor(100000 + Math.random() * 900000));
    setSentCode(c);
    setCountdown(60);
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!sentCode) {
      setError('请先获取验证码');
      return;
    }
    if (code !== sentCode) {
      setError('验证码错误');
      return;
    }

    let users: string[] = [];
    try {
      users = JSON.parse(localStorage.getItem('registeredUsers') || '[]');
    } catch {
      users = [];
    }
    const isNew = !users.includes(phone);
    if (isNew) {
      users.push(phone);
      localStorage.setItem('registeredUsers', JSON.stringify(users));
    }
    localStorage.setItem('currentUser', phone);
    navigate(isNew ? '/profile' : '/trip-survey');
  }

  return (
    <section className="login-page">
      <div className="login-card">
        <h1>TravelAgent</h1>
        <p className="muted">手机号登录</p>

        <form onSubmit={handleSubmit} className="login-form">
          <label className="login-field">
            <span>手机号</span>
            <input
              inputMode="numeric"
              maxLength={11}
              value={phone}
              onChange={(e) => setPhone(e.target.value.replace(/\D/g, ''))}
              placeholder="请输入手机号"
            />
          </label>

          <label className="login-field">
            <span>验证码</span>
            <div className="code-row">
              <input
                inputMode="numeric"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
                placeholder="请输入验证码"
              />
              <button
                type="button"
                className="code-btn"
                disabled={countdown > 0}
                onClick={sendCode}
              >
                {countdown > 0 ? `${countdown}s 后重发` : '发送验证码'}
              </button>
            </div>
          </label>

          {sentCode && <p className="dev-hint">开发模式验证码：{sentCode}</p>}
          {error && <p className="error">{error}</p>}

          <button type="submit" className="login-submit">
            登录
          </button>
        </form>
      </div>
    </section>
  );
}
