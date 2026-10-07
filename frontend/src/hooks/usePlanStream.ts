import { useCallback, useEffect, useRef, useState } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api';

export type PlanStreamEvent =
  | { type: 'node'; data: Record<string, unknown> }
  | { type: 'final'; data: Record<string, unknown> }
  | { type: 'error'; error: string };

export function usePlanStream() {
  const controllerRef = useRef<AbortController | null>(null);
  const [streaming, setStreaming] = useState(false);
  useEffect(() => () => controllerRef.current?.abort(), []);

  const start = useCallback(
    async (
      payload: Record<string, unknown>,
      onEvent: (event: PlanStreamEvent) => void,
    ) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setStreaming(true);

      try {
        const response = await fetch(`${API_BASE_URL}/plan/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });

        if (!response.ok) {
          const text = await response.text();
          onEvent({ type: 'error', error: text });
          return;
        }
        if (!(response.headers.get('content-type') || '').includes('text/event-stream')) {
          const body = await response.json() as { error?: string; detail?: string };
          onEvent({ type: 'error', error: body.error || body.detail || '规划服务没有返回有效结果，请重试。' });
          return;
        }

        const reader = response.body?.getReader();
        if (!reader) {
          onEvent({ type: 'error', error: '浏览器不支持流式响应' });
          return;
        }

        const decoder = new TextDecoder();
        let buffer = '';
        let completed = false;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          let boundary = buffer.indexOf('\n\n');
          while (boundary >= 0) {
            const raw = buffer.slice(0, boundary).trim();
            buffer = buffer.slice(boundary + 2);
            boundary = buffer.indexOf('\n\n');

            if (!raw.startsWith('data: ')) continue;
            const dataText = raw.slice(6).trim();
            if (dataText === '[DONE]') continue;
            let parsed: { type?: string; data?: Record<string, unknown>; error?: string };
            try {
              parsed = JSON.parse(dataText);
            } catch {
              continue;
            }
            if (parsed.type === 'final') {
              completed = true;
              onEvent({ type: 'final', data: parsed.data ?? {} });
            } else if (parsed.type === 'error') {
              completed = true;
              onEvent({ type: 'error', error: parsed.error ?? '未知错误' });
            } else {
              onEvent({ type: 'node', data: parsed });
            }
          }
        }
        if (!completed && !controller.signal.aborted) onEvent({ type: 'error', error: '规划连接中断，请重试。' });
      } catch (error) {
        if ((error as Error).name !== 'AbortError') {
          onEvent({ type: 'error', error: (error as Error).message });
        }
      } finally {
        if (controllerRef.current === controller) setStreaming(false);
      }
    },
    [],
  );

  const stop = useCallback(() => {
    controllerRef.current?.abort();
  }, []);

  return { start, stop, streaming };
}
