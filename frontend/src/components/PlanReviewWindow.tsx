import { useLayoutEffect, useRef, useState } from 'react';
import type { PointerEvent } from 'react';
import { PlanReviewPanel } from './PlanReviewPanel';
import type { AuditIssue, ReviewPlan, ReviewProgress } from '../lib/planReview';

type Props = {plan: ReviewPlan; busy: boolean; progress: ReviewProgress | null;
  onLocate: (issue: AuditIssue) => void; onRetry: () => void; onRepair: () => void};

export function PlanReviewWindow(props: Props) {
  const [position, setPosition] = useState(() => ({x: typeof window === 'undefined' ? 760 : Math.max(12, window.innerWidth - 404), y: 76}));
  const [hidden, setHidden] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const cardRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const clamp = () => {
      const rect = cardRef.current?.getBoundingClientRect();
      if (!rect) return;
      setPosition(previous => {
        const next = {x:Math.max(12,Math.min(previous.x,window.innerWidth - rect.width - 12)),
          y:Math.max(68,Math.min(previous.y,window.innerHeight - rect.height - 12))};
        return next.x === previous.x && next.y === previous.y ? previous : next;
      });
    };
    clamp();
    const observer = new ResizeObserver(clamp);
    if (cardRef.current) observer.observe(cardRef.current);
    window.addEventListener('resize',clamp);
    return () => { observer.disconnect(); window.removeEventListener('resize',clamp); };
  }, [hidden, expanded]);
  const drag = useRef<{x:number;y:number} | null>(null);
  function move(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    const rect = event.currentTarget.parentElement!.getBoundingClientRect();
    setPosition({x: Math.min(Math.max(12, window.innerWidth - rect.width - 12), Math.max(12, event.clientX - drag.current.x)),
      y: Math.min(Math.max(68, window.innerHeight - rect.height - 12), Math.max(68, event.clientY - drag.current.y))});
  }
  function release(event: PointerEvent<HTMLDivElement>) {
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  if (hidden) return <button type="button" className="ta-review-side-tab" onClick={() => setHidden(false)}>
    {props.busy ? '审核处理中…' : '查看行程审核'}
  </button>;
  return <aside ref={cardRef} className={`ta-floating-review ${expanded ? 'expanded' : ''}`} style={{left:`clamp(12px, ${position.x}px, calc(100vw - ${expanded ? 620 : 380}px - 12px))`,top:position.y}} aria-label="可移动审核窗口">
    <div className="ta-floating-map-handle" onPointerDown={event => {
      if ((event.target as HTMLElement).closest('button')) return;
      const rect = event.currentTarget.parentElement!.getBoundingClientRect();
      drag.current = {x:event.clientX - rect.left,y:event.clientY - rect.top};
      event.currentTarget.setPointerCapture(event.pointerId);
    }} onPointerMove={move} onPointerUp={release} onPointerCancel={release}>
      <div><span className="ta-section-kicker">行程审核</span><strong>自动检查与修复</strong></div>
      <div className="ta-floating-map-actions">
        <button type="button" onClick={() => setHidden(true)} aria-label="隐藏审核窗口">隐藏</button>
        <button type="button" onClick={() => {
          const width = Math.min(expanded ? 380 : 620, window.innerWidth - 24);
          setPosition(previous => ({...previous,x:Math.max(12,Math.min(previous.x,window.innerWidth - width - 12)),y:76}));
          setExpanded(!expanded);
        }} aria-label={expanded ? '缩小审核窗口' : '展开审核窗口'}>{expanded ? '缩小' : '展开'}</button>
      </div>
    </div>
    <div className="ta-floating-review-body"><PlanReviewPanel {...props} /></div>
  </aside>;
}
