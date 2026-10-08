import { useLayoutEffect, useRef, useState } from 'react';
import type { PointerEvent } from 'react';
import { PlanReviewPanel } from './PlanReviewPanel';
import type { AuditIssue, ReviewPlan, ReviewProgress } from '../lib/planReview';

type Position = { x: number; y: number };
type Props = {
  mapDock: Position;
  plan: ReviewPlan;
  busy: boolean;
  progress: ReviewProgress | null;
  onLocate: (issue: AuditIssue) => void;
  onRetry: () => void;
  onRepair: () => void;
};

/** Keep the compact review window immediately above the docked map. */
function getReviewDock(mapDock: Position) {
  if (typeof window === 'undefined') {
    return { position: { x: 760, y: 200 }, height: 250 };
  }
  const width = Math.min(window.innerWidth <= 760 ? 340 : 360,
    window.innerWidth - (window.innerWidth <= 760 ? 24 : 32));
  const height = Math.max(100, Math.min(270, mapDock.y - 80));
  return {
    position: {
      x: Math.max(12, Math.min(mapDock.x, window.innerWidth - width - 12)),
      y: Math.max(68, mapDock.y - height - 12),
    },
    height,
  };
}

function clampPosition(position: Position, rect: DOMRect): Position {
  const minX = 12;
  const minY = 68;
  return {
    x: Math.max(minX, Math.min(position.x, window.innerWidth - rect.width - 12)),
    y: Math.max(minY, Math.min(position.y, window.innerHeight - rect.height - 12)),
  };
}

export function PlanReviewWindow(props: Props) {
  const [hidden, setHidden] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [docked, setDocked] = useState(true);
  const [position, setPosition] = useState(() => getReviewDock(props.mapDock).position);
  const cardRef = useRef<HTMLElement | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);
  const dock = getReviewDock(props.mapDock);

  useLayoutEffect(() => {
    if (hidden) return;
    // Follow the map while docked, but allow the review window to be dragged separately.
    if (docked && !expanded) setPosition(dock.position);

    const clamp = () => {
      const rect = cardRef.current?.getBoundingClientRect();
      if (!rect) return;
      setPosition(previous => {
        const next = clampPosition(previous, rect);
        return next.x === previous.x && next.y === previous.y ? previous : next;
      });
    };
    clamp();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(clamp);
    if (cardRef.current) observer?.observe(cardRef.current);
    window.addEventListener('resize', clamp);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', clamp);
    };
  }, [hidden, expanded, docked, props.mapDock.x, props.mapDock.y]);

  function open() {
    setExpanded(false);
    setDocked(true);
    setPosition(getReviewDock(props.mapDock).position);
    setHidden(false);
  }

  function toggleExpanded() {
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    if (nextExpanded) {
      const width = Math.min(900, window.innerWidth - 48);
      const height = Math.min(550, window.innerHeight - 92);
      setDocked(false);
      setPosition({
        x: Math.max(12, Math.round((window.innerWidth - width) / 2)),
        y: Math.max(68, Math.round((window.innerHeight - height) / 2)),
      });
    } else {
      setDocked(true);
      setPosition(getReviewDock(props.mapDock).position);
    }
  }

  function move(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    const rect = cardRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition(clampPosition({
      x: event.clientX - drag.current.x,
      y: event.clientY - drag.current.y,
    }, rect));
  }

  function release(event: PointerEvent<HTMLDivElement>) {
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  if (hidden) {
    return (
      <button type="button" className="ta-map-side-tab ta-review-side-tab"
        onClick={open} aria-label="显示行程审核" title={props.busy ? '正在审核行程' : '显示行程审核'}>
        <span className="ta-map-side-arrow" aria-hidden="true">◀</span>
        <span className="ta-map-side-label">审核</span>
      </button>
    );
  }

  return (
    <aside ref={cardRef} className={`ta-floating-review ${expanded ? 'expanded' : ''}`}
      style={{ left: position.x, top: position.y, height: expanded ? undefined : dock.height }}
      aria-label="可移动审核窗口">
      <div className="ta-floating-map-handle"
        onPointerDown={(event) => {
          if ((event.target as HTMLElement).closest('button')) return;
          const rect = cardRef.current?.getBoundingClientRect();
          if (!rect) return;
          setDocked(false);
          drag.current = { x: event.clientX - rect.left, y: event.clientY - rect.top };
          event.currentTarget.setPointerCapture(event.pointerId);
        }}
        onPointerMove={move} onPointerUp={release} onPointerCancel={release}>
        <div>
          <span className="ta-section-kicker">REVIEW</span>
          <strong>行程审核</strong>
        </div>
        <div className="ta-floating-map-actions">
          <span className="ta-map-drag-hint">拖动移动</span>
          <button type="button" className="ta-map-hide-button"
            onPointerDown={event => event.stopPropagation()}
            onClick={(event) => { event.stopPropagation(); setHidden(true); }}
            aria-label="隐藏审核窗口">隐藏</button>
          <button type="button" className="ta-map-expand-button"
            onPointerDown={event => event.stopPropagation()}
            onClick={(event) => { event.stopPropagation(); toggleExpanded(); }}
            aria-label={expanded ? '缩小审核窗口' : '展开审核窗口'}>
            {expanded ? '缩小' : '展开'}
          </button>
        </div>
      </div>
      <div className="ta-floating-review-body"><PlanReviewPanel {...props} /></div>
    </aside>
  );
}
