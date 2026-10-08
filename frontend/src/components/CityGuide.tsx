import { useEffect, useState } from 'react';
import { usePlanStream } from '../hooks/usePlanStream';

export default function CityGuide({ destination, progress, status }: {
  destination: string; progress: number; status: 'running' | 'complete' | 'stopped';
}) {
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const { start, stop, streaming } = usePlanStream();
  useEffect(() => {
    void start({ destination }, event => {
      if (event.type === 'node' && typeof event.data.text === 'string') {
        const delta = event.data.text;
        setText(previous => previous + delta);
      } else if (event.type === 'error') setError('城市导览暂时不可用，不影响行程生成。');
    }, '/plan/guide/stream');
    return stop;
  }, [destination, start, stop]);
  useEffect(() => { if (status !== 'running') stop(); }, [status, stop]);
  return <section className="ta-city-guide" aria-label={`${destination}旅行导览`}>
    <strong aria-live="polite">{status === 'running' ? '线路正在生成中' : status === 'complete' ? '线路生成完成' : '线路生成已停止'}…（{progress}%）</strong>
    <div className="ta-generation-track" role="progressbar" aria-label="行程生成预计进度"
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
      <span style={{ width: `${progress}%` }} />
    </div>
    <small>预计进度 · 城市介绍仅供浏览，具体安排以生成的行程为准</small>
    <h3>✈️ {destination}旅行导览</h3>
    <div className="ta-city-guide-text" tabIndex={0} aria-label="城市介绍">{text || (streaming ? '正在了解这座城市…' : error || '城市导览已停止。')}</div>
    {text && error && <small>{error}</small>}
  </section>;
}
