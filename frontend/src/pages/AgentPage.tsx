import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '../api/client';
import type { ChatMessage, ChatResponse } from '../api/types';

export function AgentPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [plan, setPlan] = useState<ChatResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editingCardId, setEditingCardId] = useState<string | null>(null);
  const [editRequest, setEditRequest] = useState('');

  async function sendMessage(content: string) {
    const trimmed = content.trim();
    if (!trimmed || sending) return;

    const next: ChatMessage[] = [...messages, { role: 'user', content: trimmed }];
    setMessages(next);
    setInput('');
    setSending(true);
    setError(null);

    try {
      const response = await api.chat(next, sessionId);
      if (response.session_id) setSessionId(response.session_id);
      setPlan(response);
      setMessages((prev) => [...prev, { role: 'assistant', content: response.reply }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : '请求失败，请稍后重试。');
    } finally {
      setSending(false);
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    void sendMessage(input);
  }

  function selectDestination(name: string) {
    void sendMessage(name);
  }

  function submitCardEdit(e: FormEvent, cardId: string) {
    e.preventDefault();
    const request = editRequest.trim();
    if (!request) return;
    setEditingCardId(null);
    setEditRequest('');
    void sendMessage(`修改 ${cardId}：${request}`);
  }

  const cards = plan?.draft_cards ?? [];
  const destination = plan?.trip_brief.destination;
  const workflowEvents = plan?.workflow_events ?? [];
  const completedNodes = workflowEvents.filter((event) => event.event === 'node_completed');
  const failedEvent = workflowEvents.find((event) => event.event === 'workflow_failed');
  const lastNode = completedNodes[completedNodes.length - 1];

  return (
    <section className="agent-workspace">
      <div className="chat-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">TRIP RADAR</p>
            <h1>一起把旅行想清楚</h1>
          </div>
          {plan?.workflow_status && <span className="status">{plan.workflow_status}</span>}
        </div>
        <div className="chat-log">
          {messages.length === 0 && (
            <p className="muted">告诉我你想去哪里、玩几天，我会先帮你收敛选择。</p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`bubble ${m.role}`}>
              {m.content}
            </div>
          ))}
          {sending && <div className="bubble assistant muted">正在整理下一步…</div>}
        </div>
        {error && <p className="error">{error}</p>}
        <form onSubmit={handleSubmit} className="chat-form">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="输入你的旅行需求…"
            disabled={sending}
          />
          <button type="submit" disabled={sending || !input.trim()}>
            {sending ? '整理中…' : '发送'}
          </button>
        </form>
      </div>

      <aside className="plan-panel" aria-label="旅行规划结果">
        <div className="section-heading">
          <div>
            <p className="eyebrow">SHARED PLAN</p>
            <h2>当前规划</h2>
          </div>
        </div>
        {Boolean(destination) && (
          <div className="brief-strip">
            <span>目的地</span>
            <strong>{String(destination)}</strong>
          </div>
        )}

        {workflowEvents.length > 0 && (
          <section className="plan-section diagnostics-panel">
            <div className="section-label">开发诊断</div>
            <div className="diagnostics-summary">
              <span>阶段：{plan?.current_stage || '未知'}</span>
              <span>最后节点：{lastNode?.node || '尚未开始'}</span>
              <span>步骤：{completedNodes.length}</span>
              <span>耗时：{lastNode?.elapsed_ms ?? workflowEvents[workflowEvents.length - 1]?.elapsed_ms ?? 0} ms</span>
            </div>
            {failedEvent?.error && <p className="diagnostics-error">{failedEvent.error}</p>}
            <details>
              <summary>查看工作流时间线</summary>
              <ol className="workflow-timeline">
                {workflowEvents.map((event, index) => (
                  <li key={`${event.event}-${event.step ?? index}-${index}`}>
                    <strong>{event.node || event.event || 'event'}</strong>
                    <span>
                      {event.status || 'unknown'}
                      {event.elapsed_ms !== undefined ? ` · ${event.elapsed_ms} ms` : ''}
                    </span>
                    {event.writes?.length ? <small>写入：{event.writes.join(', ')}</small> : null}
                    {event.error ? <small className="diagnostics-error">错误：{event.error}</small> : null}
                  </li>
                ))}
              </ol>
            </details>
          </section>
        )}

        {plan?.candidate_destinations.length ? (
          <section className="plan-section">
            <div className="section-label">候选目的地</div>
            <div className="option-list">
              {plan.candidate_destinations.map((candidate, index) => {
                const name = String(candidate.name ?? `方案 ${index + 1}`);
                return (
                  <button
                    className="option-card"
                    key={`${name}-${index}`}
                    onClick={() => selectDestination(name)}
                    disabled={sending}
                  >
                    <strong>{name}</strong>
                    <span>{String(candidate.reason ?? '查看这个目的地方案')}</span>
                  </button>
                );
              })}
            </div>
          </section>
        ) : null}

        {plan?.time_candidates.length ? (
          <section className="plan-section">
            <div className="section-label">出行时间</div>
            <div className="option-list">
              {plan.time_candidates.map((candidate, index) => (
                <button
                  className="option-card"
                  key={`${candidate.label}-${index}`}
                  onClick={() => sendMessage(candidate.label)}
                  disabled={sending}
                >
                  <strong>{candidate.label}</strong>
                  <span>
                    {[candidate.start_date, candidate.end_date, candidate.duration_days ? `${candidate.duration_days} 天` : '']
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                  {candidate.reason && <span>{candidate.reason}</span>}
                </button>
              ))}
            </div>
          </section>
        ) : null}

        {cards.length ? (
          <section className="plan-section">
            <div className="section-label">Draft 行程</div>
            <div className="draft-list">
              {cards.map((card) => (
                <article className="draft-card" key={card.card_id}>
                  <div className="draft-card-topline">
                    <span>Day {card.day}</span>
                    <span className="card-status">
                      {card.status === 'verified'
                        ? '已验证'
                        : card.status === 'pending_revision'
                          ? '待重规划'
                          : '待验证'}
                    </span>
                  </div>
                  <h3>{card.title}</h3>
                  <p>{[card.time, card.location].filter(Boolean).join(' · ')}</p>
                  {card.description && <div className="muted">{card.description}</div>}
                  {card.evidence_refs.length === 0 && (
                    <small className="uncertain">暂为规划估计，尚未接入外部验证</small>
                  )}
                  <button
                    className="card-action"
                    onClick={() => setEditingCardId(card.card_id)}
                    disabled={sending}
                  >
                    {editingCardId === card.card_id ? '收起修改' : '修改这张卡'}
                  </button>
                  {editingCardId === card.card_id && (
                    <form className="card-edit-form" onSubmit={(e) => submitCardEdit(e, card.card_id)}>
                      <input
                        value={editRequest}
                        onChange={(e) => setEditRequest(e.target.value)}
                        placeholder="例如：换成更便宜的餐厅"
                        aria-label={`修改 ${card.title}`}
                        autoFocus
                        disabled={sending}
                      />
                      <button type="submit" disabled={sending || !editRequest.trim()}>
                        提交修改
                      </button>
                    </form>
                  )}
                </article>
              ))}
            </div>
          </section>
        ) : (
          <p className="muted plan-empty">完成目的地和时间选择后，这里会出现可修改的行程卡片。</p>
        )}

        {plan?.revision_comparison.length ? (
          <section className="plan-section revision-section">
            <div className="section-label">修改对比</div>
            {plan.revision_comparison.map((revision) => (
              <div className="revision-card" key={revision.card_id}>
                <strong>{revision.card_id}</strong>
                <span>{revision.before.title} → {revision.after?.title ?? '等待新版本'}</span>
                {revision.after && (
                  <small className="uncertain">
                    新版本仍需确认，且尚未完成外部事实验证
                  </small>
                )}
              </div>
            ))}
            <div className="revision-actions">
              <button
                className="card-action primary-action"
                onClick={() => sendMessage('接受修改')}
                disabled={sending}
              >
                采用新版本
              </button>
              <button
                className="card-action"
                onClick={() => sendMessage('保留旧版本')}
                disabled={sending}
              >
                保留旧版本
              </button>
            </div>
          </section>
        ) : null}
      </aside>
    </section>
  );
}

