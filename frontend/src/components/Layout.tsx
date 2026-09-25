import { Link, Outlet } from 'react-router-dom';

export function Layout() {
  return (
    <div className="app-shell">
      <header className="app-header">
        <Link to="/" className="brand">
          TravelAgent
        </Link>
        <nav>
          <Link to="/trips">我的行程</Link>
          <Link to="/agent">AI 助手</Link>
        </nav>
      </header>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}

