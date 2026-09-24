export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export const SESSION_EXPIRED_EVENT = "flawless:session-expired";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/backend${path}`, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      ...(init?.body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });
  const body = await response.json().catch(() => null);
  init?.signal?.throwIfAborted();
  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/cabinet-api/login" &&
      typeof window !== "undefined"
    ) {
      window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
    }
    const detail = body?.detail;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : body?.message || "Не удалось выполнить запрос. Попробуйте ещё раз.",
      response.status,
    );
  }
  return body as T;
}

export const money = (value: string | number, digits = 2) =>
  new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
export const count = (value: number) =>
  new Intl.NumberFormat("ru-RU").format(value);
export const dateLabel = (value: string) =>
  new Date(
    value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
  ).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
export const modelInfo = (alias: string) => {
  if (alias.includes("claude"))
    return {
      name: "Claude Sonnet",
      vendor: "Anthropic",
      mark: "✳",
      color: "peach",
      description: "Тексты, код и глубокая работа с контекстом",
      tag: "Для сложных задач",
    };
  if (alias.includes("lite"))
    return {
      name: "Gemini Flash Lite",
      vendor: "Google",
      mark: "✦",
      color: "cyan",
      description: "Повседневные задачи с небольшим бюджетом",
      tag: "Экономичный",
    };
  if (alias.includes("gemini"))
    return {
      name: "Gemini Flash",
      vendor: "Google",
      mark: "✦",
      color: "blue",
      description: "Быстрый старт для идей и черновиков",
      tag: "Быстрый",
    };
  if (alias.includes("gpt"))
    return {
      name: alias === "gpt-5-mini" ? "GPT-5 mini" : alias,
      vendor: "OpenAI",
      mark: "◎",
      color: "mint",
      description: "Универсальный помощник на каждый день",
      tag: "Универсальный",
    };
  return {
    name: alias,
    vendor: "Модель",
    mark: "◇",
    color: "violet",
    description: "Доступна в вашем рабочем пространстве",
    tag: "В каталоге",
  };
};
