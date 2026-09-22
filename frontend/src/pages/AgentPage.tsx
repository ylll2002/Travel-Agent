import { useState } from 'react';
import type { FormEvent } from 'react';
import { api } from '../api/client';
import type { ChatMessage } from '../api/types';

export function AgentPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const content = input.trim();
    if (!content) return;

    const next: ChatMessage[] = [...messages, { role: 'user', content }];
    setMessages(next);
    setInput('');
    setSending(true);

    try {
      const { reply } = await api.chat(next);
      setMessages((prev) => [...prev, { role: 'assistant', content: reply }]);
    } finally {
      setSending(false);
    }
  }

  return (
    <section className="chat-panel">
      <h1>AI 旅行助手</h1>
      <div className="chat-log">
        {messages.length === 0 && (
          <p className="muted">告诉我你想去哪里、玩几天，我来帮你规划。</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            {m.content}
          </div>
        ))}
      </div>
      <form onSubmit={handleSubmit} className="chat-form">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入你的旅行需求…"
        />
        <button type="submit" disabled={sending}>
          {sending ? '思考中…' : '发送'}
        </button>
      </form>
    </section>
  );
}

