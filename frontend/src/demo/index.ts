/**
 * Offline demo mode: answers every `/api/*` request from in-memory fixtures so
 * the frontend runs without a backend or any ChatGPT account.
 *
 * Wired up in main.tsx for `npm run demo` (Vite mode "demo"); production builds drop it.
 *
 * Any password logs in. All data is invented (example.com addresses, cards
 * ending in 0000/4242, patterned ids) and every timestamp is relative to the
 * moment of install, so the panel always looks current. State lives only in
 * memory and resets on reload.
 */
import { createDemoDb, type DemoDb } from './db';
import type { DemoRequest, DemoResponse } from './http';
import { dispatch } from './router';

const INSTALLED = Symbol.for('teamboss.demoApiInstalled');

function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The operation was aborted.', 'AbortError'));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        reject(new DOMException('The operation was aborted.', 'AbortError'));
      },
      { once: true },
    );
  });
}

function requestUrl(input: RequestInfo | URL): URL {
  const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  return new URL(raw, window.location.href);
}

async function readBody(input: RequestInfo | URL, init?: RequestInit): Promise<unknown> {
  let text: string | null = null;
  if (init?.body !== undefined && init.body !== null) {
    text = typeof init.body === 'string' ? init.body : null;
  } else if (input instanceof Request) {
    text = await input.clone().text();
  }
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function toResponse(result: DemoResponse): Response {
  if (result.status === 204) return new Response(null, { status: 204 });
  return new Response(JSON.stringify(result.body ?? null), {
    status: result.status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export function installDemoApi(): void {
  const host = window as typeof window & { [INSTALLED]?: DemoDb };
  if (host[INSTALLED]) return;

  const db = createDemoDb(Date.now());
  host[INSTALLED] = db;
  const realFetch = window.fetch.bind(window);

  const demoFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = requestUrl(input);
    if (!url.pathname.startsWith('/api/')) return realFetch(input, init);

    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase();
    const signal = init?.signal ?? (input instanceof Request ? input.signal : null);
    const request: DemoRequest = {
      method,
      path: url.pathname.replace(/\/+$/, ''),
      query: url.searchParams,
      body: await readBody(input, init),
    };

    await delay(150 + Math.floor(Math.random() * 250), signal);
    try {
      return toResponse(dispatch(db, request));
    } catch (error) {
      console.error('[demo api] handler failed', request.method, request.path, error);
      return toResponse({ status: 500, body: { detail: '演示数据处理出错' } });
    }
  };

  window.fetch = demoFetch as typeof window.fetch;
  console.info('[demo api] TeamBoss demo mode: /api/* is served from in-memory fixtures.');
}
