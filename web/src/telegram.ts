/** Мини-апп Telegram: цвета шапки, вибрация, кнопка «Назад», ссылки. Вне Telegram всё это — no-op. */

type BackButton = { show(): void; hide(): void; onClick(cb: () => void): void; offClick(cb: () => void): void };
type Haptic = {
  impactOccurred(style: "light" | "medium" | "heavy" | "rigid" | "soft"): void;
  notificationOccurred(type: "error" | "success" | "warning"): void;
  selectionChanged(): void;
};
type TgWebApp = {
  initData: string;
  initDataUnsafe?: { user?: { id: number; first_name?: string; photo_url?: string } };
  version: string;
  ready(): void;
  expand(): void;
  isVersionAtLeast(v: string): boolean;
  setHeaderColor?(c: string): void;
  setBackgroundColor?(c: string): void;
  setBottomBarColor?(c: string): void;
  disableVerticalSwipes?(): void;
  enableVerticalSwipes?(): void;
  BackButton: BackButton;
  HapticFeedback?: Haptic;
  openTelegramLink(url: string): void;
};

const w = window as unknown as { Telegram?: { WebApp?: TgWebApp } };
export const tg: TgWebApp | null = w.Telegram?.WebApp?.initData ? w.Telegram.WebApp : null;
export const tgUser = tg?.initDataUnsafe?.user ?? null;

const BG = "#0b0b0d";

/** Доска всегда в тёмной теме: красим шапку и фон Telegram под неё. */
export function initTelegram(): void {
  if (!tg) return;
  tg.ready();
  tg.expand();
  try {
    if (tg.isVersionAtLeast("6.1")) {
      tg.setHeaderColor?.(BG);
      tg.setBackgroundColor?.(BG);
    }
    if (tg.isVersionAtLeast("7.10")) tg.setBottomBarColor?.(BG);
    // чтобы прокрутка списка не сворачивала мини-апп
    if (tg.isVersionAtLeast("7.7")) tg.disableVerticalSwipes?.();
  } catch {
    /* старый клиент — просто без этого */
  }
}

/** Тактильный отклик — то самое «приятно нажимать». */
export const haptic = {
  tap: () => tg?.HapticFeedback?.impactOccurred("light"),
  soft: () => tg?.HapticFeedback?.impactOccurred("soft"),
  select: () => tg?.HapticFeedback?.selectionChanged(),
  success: () => tg?.HapticFeedback?.notificationOccurred("success"),
  warn: () => tg?.HapticFeedback?.notificationOccurred("warning"),
};

/** t.me-ссылки внутри мини-аппа открываем средствами Telegram, а не браузером. */
export function openLink(url: string): boolean {
  if (tg && url.startsWith("https://t.me/")) {
    tg.openTelegramLink(url);
    return true;
  }
  return false;
}
