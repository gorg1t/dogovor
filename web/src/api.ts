export type Quote = { text: string; author: string; ts: string };

export type Task = {
  id: number;
  title: string;
  assignee: string;
  assignee_user_id: number | null;
  deadline: string | null;
  deadline_text: string | null;
  status: "open" | "done" | "deleted";
  kind: string;
  edited: boolean;
  quote: Quote;
  context: Quote[];        // сообщения до обещания: задание, на которое ответили
  context_after: Quote[];  // пара сообщений сразу после
  history: { ts: string; text: string }[];
  tg_link: string | null;
  confirmed_by: string;
  requested_by: string | null;  // кто предложил задачу
  deadline_time: string | null;  // «сегодня к 18:00»
  urgent: boolean;  // «срочно»
  managers: number[];  // кто может менять: исполнитель, предложивший, автор (пусто — любой)
  created_at: string;
  done_at: string | null;
};

export type OpenItem = {
  key: string;
  task: string;
  deadline: string | null;
  deadline_text: string | null;
  requested_by: string;
  quote: string;
  ts: string;
  tg_link: string | null;
};

export type Board = {
  chat: { id: number; title: string; checkpoint: string | null };
  today: string;
  me: { id: number; name: string } | null;  // null — вход не через Telegram: только просмотр
  members: { user_id: number; name: string; username: string | null }[];
  tasks: Task[];
  open_items: OpenItem[];
};

let token = new URLSearchParams(window.location.search).get("t") ?? "";

export const hasToken = () => token !== "";
export const setToken = (t: string) => { token = t; };

export type TgAuthResult = {
  token?: string;
  chats?: { id: number; title: string; token: string }[];
  user: { id: number; name: string };
};

/** Вход из мини-аппа: сервер проверяет подпись initData и членство в чате. */
export async function tgAuth(initData: string): Promise<TgAuthResult> {
  const res = await fetch("/api/tg-auth", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ init_data: initData }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ?? `Ошибка ${res.status}`);
  return body;
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${path}${sep}t=${encodeURIComponent(token)}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `Ошибка ${res.status}`);
  }
  return res.json();
}

export const fetchBoard = () => call<Board>("/api/board");

export type TaskPatch = Partial<{ status: string; title: string; deadline: string; clear_deadline: boolean; assignee: string }>;

export const patchTask = (id: number, patch: TaskPatch) =>
  call<Task>(`/api/tasks/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export const claimOpenItem = (key: string) => call<Task>("/api/open-items/claim", { method: "POST", body: JSON.stringify({ key }) });

export const deleteTask = (id: number) => call<Task>(`/api/tasks/${id}`, { method: "DELETE" });

// ---------- встречи ----------
export type Rsvp = "yes" | "maybe" | "no";
export type Attendee = { user_id: number; name: string; status: Rsvp };

export type EventItem = {
  id: number;
  title: string;
  day: string | null;        // YYYY-MM-DD
  starts_at: string | null;  // ISO с часовым поясом, если время названо
  when_text: string | null;
  place: string | null;
  status: "proposed" | "planned";
  created_by: string;
  quote: string;
  tg_link: string | null;
  attendees: Attendee[];
};

export type EventsResponse = { today: string; now: string; me: { id: number; name: string } | null; events: EventItem[] };

export const fetchEvents = () => call<EventsResponse>("/api/events");
export const rsvpEvent = (id: number, status: Rsvp) =>
  call<EventItem>(`/api/events/${id}/rsvp`, { method: "POST", body: JSON.stringify({ status }) });
export const cancelEvent = (id: number) => call<{ ok: boolean }>(`/api/events/${id}`, { method: "DELETE" });
