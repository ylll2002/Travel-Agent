import { useNavigate } from 'react-router-dom';

export function HomePage() {
  const navigate = useNavigate();

  return (
    <div className="agent-page home-page">
      <div className="home-center">
        <header className="agent-hero">
          <span className="agent-kicker">TravelAgent · 你的 AI 旅行规划师</span>
          <h1>
            准备好开始
            <br />
            下一次旅行了吗？
          </h1>
          <p>从一次对话开始，为你生成专属的旅行方案。</p>
        </header>

        <button
          type="button"
          className="agent-primary-button home-start-button"
          onClick={() => navigate('/agent')}
        >
          开始本次旅行
          <span aria-hidden="true">→</span>
        </button>
      </div>
    </div>
  );
}

export default HomePage;
