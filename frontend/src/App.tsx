import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { AgentPage } from './pages/AgentPage';
import { HomePage } from './pages/HomePage';
import { LoginPage } from './pages/LoginPage';
import { ProfilePage } from './pages/ProfilePage';
import { TripDetailPage } from './pages/TripDetailPage';
import { TripSurveyPage } from './pages/TripSurveyPage';
import { TripsPage } from './pages/TripsPage';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<HomePage />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/trips" element={<TripsPage />} />
          <Route path="/trips/:id" element={<TripDetailPage />} />
          <Route path="/agent" element={<AgentPage />} />
          <Route path="/profile" element={<ProfilePage />} />
          <Route path="/trip-survey" element={<TripSurveyPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
