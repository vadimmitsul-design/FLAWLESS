"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCheck,
  CircleAlert,
  Code2,
  Copy,
  Download,
  FileText,
  KeyRound,
  LoaderCircle,
  MessageSquare,
  Paperclip,
  Plus,
  Search,
  ShieldCheck,
  Sparkles,
  Trash2,
  Wallet,
  X,
} from "lucide-react";
import { api, ApiError, count, dateLabel, modelInfo, money } from "@/lib/api";
import type { ChatMessage, Dashboard, Usage, View } from "@/lib/types";
import "./workspace-views.css";

type WorkspaceProps = {
  view: View;
  data: Dashboard;
  onRefresh: () => Promise<void>;
  onNavigate: (view: View) => void;
  onStartChat: (model: string, prompt?: string, id?: number) => void;
  initialModel: string;
  initialPrompt: string;
  conversationId: number | null;
  onNotice: (message: string) => void;
};

const errorMessage = (error: unknown) =>
  error instanceof Error
    ? error.message
    : "Не удалось выполнить запрос. Попробуйте ещё раз.";

function ErrorNotice({ children }: { children: ReactNode }) {
  return (
    <div className="wv-error" role="alert">
      <CircleAlert size={18} />
      <span>{children}</span>
    </div>
  );
}

function EmptyState({
  icon,
  title,
  children,
}: {
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="wv-empty">
      <div className="wv-empty-icon">{icon}</div>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}

function ViewHeading({
  title,
  description,
  action,
}: {
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="wv-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}

function Status({ status }: { status: string }) {
  const labels: Record<string, string> = {
    success: "Выполнен",
    failed: "Ошибка",
    pending: "В процессе",
    requested: "На проверке",
    confirmed: "Зачислено",
    rejected: "Отклонено",
  };
  return (
    <span className={`wv-status wv-status-${status}`}>
      <i aria-hidden="true" />
      {labels[status] || status}
    </span>
  );
}

async function copyText(text: string, onNotice: WorkspaceProps["onNotice"]) {
  try {
    await navigator.clipboard.writeText(text);
    onNotice("Скопировано в буфер обмена");
  } catch {
    onNotice(
      "Не удалось скопировать автоматически. Выделите текст и скопируйте вручную.",
    );
  }
}

export function WorkspaceView(props: WorkspaceProps) {
  switch (props.view) {
    case "chat":
      return (
        <ChatView
          key={`${props.conversationId}:${props.initialModel}:${props.initialPrompt}`}
          {...props}
        />
      );
    case "models":
      return <ModelsView {...props} />;
    case "usage":
      return <UsageView {...props} />;
    case "keys":
      return <KeysView {...props} />;
    case "wallet":
      return <WalletView {...props} />;
    case "settings":
      return <SettingsView {...props} />;
    default:
      return null;
  }
}

function ChatView({
  data,
  onRefresh,
  onStartChat,
  initialModel,
  initialPrompt,
  conversationId,
  onNotice,
}: WorkspaceProps) {
  const [model, setModel] = useState(initialModel || data.models[0] || "");
  const [message, setMessage] = useState(initialPrompt);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activeId, setActiveId] = useState(conversationId);
  const [title, setTitle] = useState("Новый диалог");
  const [loading, setLoading] = useState(conversationId !== null);
  const [historyError, setHistoryError] = useState("");
  const [historyAttempt, setHistoryAttempt] = useState(0);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [attachment, setAttachment] = useState<File | null>(null);
  const [historyQuery, setHistoryQuery] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const sendAbortRef = useRef<AbortController | null>(null);
  const info = modelInfo(model);
  const composerDisabled = sending || loading || Boolean(historyError);

  useEffect(() => {
    if (conversationId === null) return;
    const controller = new AbortController();
    api<{ id: number; title: string; model: string; messages: ChatMessage[] }>(
      `/cabinet-api/conversations/${conversationId}`,
      { signal: controller.signal },
    )
      .then((conversation) => {
        if (controller.signal.aborted) return;
        setMessages(conversation.messages);
        setModel(conversation.model);
        setTitle(conversation.title);
        setHistoryError("");
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setHistoryError(errorMessage(err));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [conversationId, historyAttempt]);

  useEffect(() => () => sendAbortRef.current?.abort(), []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "nearest" });
  }, [messages, sending]);

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (
      composerDisabled ||
      sendAbortRef.current ||
      (!message.trim() && !attachment) ||
      !model
    )
      return;
    const controller = new AbortController();
    sendAbortRef.current = controller;
    setSending(true);
    setError("");
    const text = message.trim();
    const file = attachment;
    const form = new FormData();
    form.append("model", model);
    form.append("message", text);
    if (activeId !== null) form.append("conversation_id", String(activeId));
    if (file) form.append("file", file);
    try {
      const response = await api<{
        conversation_id: number;
        conversation_title: string;
        reply: string;
      }>("/chat/send", {
        method: "POST",
        body: form,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setActiveId(response.conversation_id);
      setTitle(response.conversation_title);
      setMessages((previous) => [
        ...previous,
        { role: "user", text, attachment_name: file?.name || null },
        { role: "assistant", text: response.reply, attachment_name: null },
      ]);
      setMessage("");
      setAttachment(null);
      if (fileInput.current) fileInput.current.value = "";
      await onRefresh().catch(() => {
        if (!controller.signal.aborted) {
          onNotice(
            "Ответ получен. Обновите обзор, чтобы увидеть актуальный баланс.",
          );
        }
      });
      if (!controller.signal.aborted) textarea.current?.focus();
    } catch (err) {
      if (controller.signal.aborted) return;
      const detail =
        err instanceof ApiError && err.status === 502
          ? "Модель сейчас не отвечает. Попробуйте другую модель или повторите запрос позже."
          : errorMessage(err);
      setError(detail);
      await onRefresh().catch(() => undefined);
    } finally {
      if (sendAbortRef.current === controller) sendAbortRef.current = null;
      if (!controller.signal.aborted) setSending(false);
    }
  }

  function selectFile(file: File | undefined) {
    if (!file) return;
    const extension = file.name.split(".").pop()?.toLowerCase();
    if (file.size > 5 * 1024 * 1024) {
      setError("Файл слишком большой. Максимальный размер — 5 МБ.");
      return;
    }
    if (
      !file.type.startsWith("image/") &&
      !["txt", "md", "csv", "json", "log"].includes(extension || "")
    ) {
      setError(
        "Прикрепите изображение или текстовый файл: TXT, MD, CSV, JSON, LOG.",
      );
      return;
    }
    setError("");
    setAttachment(file);
  }

  const history = data.conversations.filter((item) =>
    item.title.toLocaleLowerCase().includes(historyQuery.toLocaleLowerCase()),
  );
  return (
    <div className="wv-chat-layout">
      <aside className="wv-chat-history" aria-label="История диалогов">
        <button
          className="button button-primary wv-new-chat"
          disabled={sending}
          onClick={() => onStartChat(model)}
        >
          <Plus size={17} />
          Новый диалог
        </button>
        <label className="wv-search wv-history-search">
          <Search size={16} />
          <input
            value={historyQuery}
            onChange={(event) => setHistoryQuery(event.target.value)}
            placeholder="Найти диалог"
            aria-label="Поиск по диалогам"
          />
        </label>
        <p className="wv-small-label">Последние диалоги</p>
        <div className="wv-history-list">
          {history.map((conversation) => (
            <button
              key={conversation.id}
              className={`wv-history-item ${activeId === conversation.id ? "is-active" : ""}`}
              disabled={sending}
              onClick={() =>
                onStartChat(conversation.model, "", conversation.id)
              }
              aria-current={activeId === conversation.id ? "page" : undefined}
            >
              <MessageSquare size={16} />
              <span>
                <strong>{conversation.title}</strong>
                <small>
                  {modelInfo(conversation.model).name} ·{" "}
                  {dateLabel(conversation.updated_at)}
                </small>
              </span>
            </button>
          ))}
          {history.length === 0 && (
            <p className="wv-history-empty">
              {historyQuery
                ? "Совпадений нет. Попробуйте другой запрос."
                : "Ваши диалоги появятся здесь после первого сообщения."}
            </p>
          )}
        </div>
        <div className="wv-history-note">
          <ShieldCheck size={17} />
          <span>История доступна только вашему аккаунту.</span>
        </div>
      </aside>
      <section className="wv-chat-main" aria-label="Чат с моделью">
        <header className="wv-chat-top">
          <div>
            <span className={`model-mark ${info.color}`}>{info.mark}</span>
            <div>
              <h2>{title}</h2>
              <p>{info.name}</p>
            </div>
          </div>
          <button
            className="icon-button wv-mobile-new"
            aria-label="Новый диалог"
            disabled={sending}
            onClick={() => onStartChat(model)}
          >
            <Plus size={20} />
          </button>
        </header>
        <details className="wv-mobile-conversations">
          <summary>
            <MessageSquare size={15} />
            История диалогов<span>{data.conversations.length}</span>
          </summary>
          <div>
            {data.conversations.length ? (
              data.conversations.map((conversation) => (
                <button
                  className={`wv-history-item ${activeId === conversation.id ? "is-active" : ""}`}
                  key={conversation.id}
                  disabled={sending}
                  onClick={() =>
                    onStartChat(conversation.model, "", conversation.id)
                  }
                >
                  <MessageSquare size={15} />
                  <span>
                    <strong>{conversation.title}</strong>
                    <small>
                      {modelInfo(conversation.model).name} ·{" "}
                      {dateLabel(conversation.updated_at)}
                    </small>
                  </span>
                </button>
              ))
            ) : (
              <p className="wv-history-empty">
                История появится после первого сообщения.
              </p>
            )}
          </div>
        </details>
        <div className="wv-messages" aria-busy={loading || sending}>
          {loading ? (
            <div className="wv-chat-skeleton" aria-label="Загрузка диалога">
              <span />
              <span />
              <span />
            </div>
          ) : historyError ? (
            <div className="wv-chat-load-error">
              <EmptyState
                icon={<CircleAlert size={26} />}
                title="Не удалось открыть диалог"
              >
                История не загружена. Повторите загрузку или начните новый
                диалог.
              </EmptyState>
              <ErrorNotice>{historyError}</ErrorNotice>
              <div className="wv-chat-recovery-actions">
                <button
                  className="button button-primary"
                  onClick={() => {
                    setLoading(true);
                    setHistoryError("");
                    setHistoryAttempt((attempt) => attempt + 1);
                  }}
                >
                  Повторить загрузку
                </button>
                <button
                  className="button button-secondary"
                  onClick={() => onStartChat(model)}
                >
                  <Plus size={16} />
                  Новый диалог
                </button>
              </div>
            </div>
          ) : messages.length === 0 ? (
            <div className="wv-chat-welcome">
              <div className="wv-chat-spark">
                <Sparkles size={30} />
              </div>
              <h2>От идеи — к результату.</h2>
              <p>
                Обсудите задачу, разберите код или найдите
                <br className="wv-desktop-break" /> новый подход. Начните с
                того, что важно вам.
              </p>
              <div className="wv-prompt-options">
                {[
                  {
                    icon: Code2,
                    label: "Разобрать код",
                    text: "Помоги разобраться в коде. Я пришлю фрагмент, а ты объясни его логику и предложи улучшения.",
                  },
                  {
                    icon: Sparkles,
                    label: "Найти идею",
                    text: "Помоги придумать идею для проекта. Сначала задай мне три вопроса о задаче и аудитории.",
                  },
                  {
                    icon: FileText,
                    label: "Улучшить текст",
                    text: "Помоги улучшить мой текст: сделать его яснее, короче и убедительнее. Я пришлю черновик.",
                  },
                ].map((prompt) => (
                  <button
                    key={prompt.label}
                    onClick={() => {
                      setMessage(prompt.text);
                      textarea.current?.focus();
                    }}
                  >
                    <prompt.icon size={18} />
                    <span>{prompt.label}</span>
                    <ArrowUpRight size={15} />
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((item, index) => (
              <article
                className={`wv-message wv-message-${item.role === "user" ? "user" : "assistant"}`}
                key={`${index}-${item.role}`}
              >
                <div className="wv-message-author">
                  {item.role === "user" ? (
                    <span className="wv-user-dot">
                      {data.customer.name.slice(0, 1) || "В"}
                    </span>
                  ) : (
                    <span className={`model-mark ${info.color}`}>
                      {info.mark}
                    </span>
                  )}
                  <strong>{item.role === "user" ? "Вы" : info.name}</strong>
                  {item.role !== "user" && (
                    <button
                      className="icon-button"
                      aria-label="Скопировать ответ"
                      onClick={() => void copyText(item.text, onNotice)}
                    >
                      <Copy size={14} />
                    </button>
                  )}
                </div>
                <div className="wv-message-text">{item.text}</div>
                {item.attachment_name && (
                  <span className="wv-message-file">
                    <Paperclip size={14} />
                    {item.attachment_name}
                  </span>
                )}
              </article>
            ))
          )}
          {sending && (
            <div className="wv-thinking" role="status">
              <LoaderCircle size={17} className="wv-spinning" />
              {info.name} готовит ответ…
            </div>
          )}
          <div ref={bottom} />
        </div>
        <div className="wv-composer-wrap">
          {error && <ErrorNotice>{error}</ErrorNotice>}
          <form
            className="wv-composer"
            onSubmit={(event) => void sendMessage(event)}
          >
            {attachment && (
              <div className="wv-attachment">
                <Paperclip size={14} />
                <span>{attachment.name}</span>
                <button
                  type="button"
                  className="icon-button"
                  disabled={composerDisabled}
                  onClick={() => {
                    setAttachment(null);
                    if (fileInput.current) fileInput.current.value = "";
                  }}
                  aria-label="Убрать вложение"
                >
                  <X size={14} />
                </button>
              </div>
            )}
            <textarea
              ref={textarea}
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="Какая у вас задача?"
              aria-label="Сообщение модели"
              disabled={composerDisabled}
              rows={3}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
            <div className="wv-composer-tools">
              <div>
                <input
                  ref={fileInput}
                  type="file"
                  accept="image/*,.txt,.md,.csv,.json,.log"
                  className="wv-file-input"
                  disabled={composerDisabled}
                  onChange={(event) => selectFile(event.target.files?.[0])}
                  tabIndex={-1}
                />
                <button
                  className="icon-button"
                  type="button"
                  disabled={composerDisabled}
                  onClick={() => fileInput.current?.click()}
                  title="Изображение или текстовый файл, до 5 МБ"
                  aria-label="Прикрепить файл"
                >
                  <Paperclip size={19} />
                </button>
                <select
                  className="wv-model-select"
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  aria-label="Модель для сообщения"
                  disabled={composerDisabled || data.models.length === 0}
                >
                  {[...new Set([model, ...data.models])]
                    .filter(Boolean)
                    .map((alias) => (
                      <option value={alias} key={alias}>
                        {modelInfo(alias).name}
                      </option>
                    ))}
                </select>
              </div>
              <button
                className="button button-primary wv-send"
                type="submit"
                aria-label="Отправить сообщение"
                disabled={
                  composerDisabled || (!message.trim() && !attachment) || !model
                }
              >
                {sending ? (
                  <LoaderCircle className="wv-spinning" size={19} />
                ) : (
                  <ArrowUpRight size={21} />
                )}
              </button>
            </div>
          </form>
          <p className="wv-composer-help">
            <span>Enter — отправить · Shift + Enter — новая строка</span>
            <span>До 5 МБ · изображения и текст</span>
          </p>
        </div>
      </section>
    </div>
  );
}

function ModelsView({ data, onStartChat }: WorkspaceProps) {
  const [query, setQuery] = useState("");
  const models = data.models.filter((model) =>
    `${model} ${modelInfo(model).vendor}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <div className="wv-page">
      <ViewHeading
        title="Модель под вашу задачу"
        description="Один баланс, знакомый интерфейс и свобода переключаться."
      />
      <div className="wv-models-toolbar">
        <label className="wv-search">
          <Search size={17} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Найти модель или разработчика"
            aria-label="Поиск моделей"
          />
        </label>
        <span className="muted">{data.models.length} в каталоге</span>
      </div>
      <div className="wv-model-catalog">
        {models.map((alias) => {
          const info = modelInfo(alias);
          return (
            <article key={alias} className="wv-model-entry">
              <div className={`model-mark wv-model-large ${info.color}`}>
                {info.mark}
              </div>
              <div className="wv-model-description">
                <span className="wv-model-vendor">{info.vendor}</span>
                <h2>{info.name}</h2>
                <p>{info.description}</p>
                <code>{alias}</code>
              </div>
              <div className="wv-model-action">
                <span className="wv-model-tag">{info.tag}</span>
                <button
                  className="button button-secondary"
                  onClick={() => onStartChat(alias)}
                >
                  Начать диалог
                  <ArrowUpRight size={17} />
                </button>
              </div>
            </article>
          );
        })}
      </div>
      {models.length === 0 && (
        <EmptyState
          icon={<Search size={25} />}
          title={query ? "Такой модели нет" : "Каталог пока пуст"}
        >
          {query
            ? "Попробуйте название разработчика или другой поисковый запрос."
            : "Модели появятся после настройки провайдеров и тарифов администратором."}
        </EmptyState>
      )}
      <div className="wv-inline-note">
        <ShieldCheck size={18} />
        <p>
          Расходы учитываются по фактическому использованию. История запросов и
          списаний всегда доступна в разделе «Использование».
        </p>
      </div>
    </div>
  );
}

function UsageView({ data }: WorkspaceProps) {
  const [query, setQuery] = useState("");
  const [model, setModel] = useState("all");
  const [status, setStatus] = useState("all");
  const rows = useMemo(
    () =>
      data.usage.filter(
        (item) =>
          (model === "all" || item.model === model) &&
          (status === "all" || item.status === status) &&
          `${item.model} ${item.id}`
            .toLowerCase()
            .includes(query.toLowerCase()),
      ),
    [data.usage, model, status, query],
  );
  const total = rows.reduce((sum, item) => sum + Number(item.charged_rub), 0);

  function exportCsv() {
    const cell = (value: string | number) =>
      `"${String(value)
        .replace(/^[=+@-]/, "'$&")
        .replaceAll('"', '""')}"`;
    const csv = [
      [
        "ID",
        "Дата",
        "Модель",
        "Входящие токены",
        "Исходящие токены",
        "Стоимость, ₽",
        "Статус",
      ],
      ...rows.map((item) => [
        item.id,
        item.created_at,
        item.model,
        item.input_tokens,
        item.output_tokens,
        item.charged_rub,
        item.status,
      ]),
    ]
      .map((row) => row.map(cell).join(";"))
      .join("\r\n");
    const url = URL.createObjectURL(
      new Blob(["\uFEFF", csv], { type: "text/csv;charset=utf-8;" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `flawless-recent-usage-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="wv-page">
      <ViewHeading
        title="Всё под контролем"
        description="Последние 50 запросов: модели, токены и точная стоимость."
        action={
          <button
            className="button button-secondary"
            onClick={exportCsv}
            disabled={rows.length === 0}
          >
            <Download size={16} />
            Экспорт CSV
          </button>
        }
      />
      <div className="wv-usage-summary">
        <div>
          <span>Запросов в выборке</span>
          <strong>{count(rows.length)}</strong>
        </div>
        <div>
          <span>Токенов обработано</span>
          <strong>
            {count(
              rows.reduce(
                (sum, row) => sum + row.input_tokens + row.output_tokens,
                0,
              ),
            )}
          </strong>
        </div>
        <div>
          <span>Расход по выборке</span>
          <strong>
            {money(total)} <small>₽</small>
          </strong>
        </div>
      </div>
      <div className="wv-filter-bar">
        <label className="wv-search">
          <Search size={17} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Модель или ID запроса"
            aria-label="Поиск запросов"
          />
        </label>
        <select
          className="field"
          aria-label="Фильтр по модели"
          value={model}
          onChange={(event) => setModel(event.target.value)}
        >
          <option value="all">Все модели</option>
          {[...new Set(data.usage.map((item) => item.model))].map((alias) => (
            <option key={alias} value={alias}>
              {modelInfo(alias).name}
            </option>
          ))}
        </select>
        <select
          className="field"
          aria-label="Фильтр по статусу"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="all">Любой статус</option>
          <option value="success">Выполнен</option>
          <option value="pending">В процессе</option>
          <option value="failed">Ошибка</option>
        </select>
      </div>
      <div className="wv-table-wrap">
        <table className="wv-table">
          <thead>
            <tr>
              <th>Модель / запрос</th>
              <th>Дата</th>
              <th>Токены</th>
              <th>Стоимость</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <UsageRow key={item.id} item={item} />
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <EmptyState icon={<Search size={24} />} title="Запросы не найдены">
            {data.usage.length
              ? "Измените условия поиска или сбросьте фильтры."
              : "Отправьте первое сообщение в чате — расход появится здесь."}
          </EmptyState>
        )}
      </div>
      <p className="wv-table-caption">
        Время отображается в вашем часовом поясе. CSV содержит только показанную
        выборку из последних 50 запросов.
      </p>
    </div>
  );
}

function UsageRow({ item }: { item: Usage }) {
  const info = modelInfo(item.model);
  const createdAt = new Date(
    item.created_at.endsWith("Z") || /[+-]\d\d:\d\d$/.test(item.created_at)
      ? item.created_at
      : `${item.created_at}Z`,
  );
  return (
    <tr>
      <td>
        <div className="wv-table-model">
          <span className={`model-mark ${info.color}`}>{info.mark}</span>
          <div>
            <strong>{info.name}</strong>
            <small className="mono">#{String(item.id).slice(0, 8)}</small>
          </div>
        </div>
      </td>
      <td className="wv-nowrap">
        {dateLabel(item.created_at)}
        <small>
          {createdAt.toLocaleTimeString("ru-RU", {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </small>
      </td>
      <td className="mono wv-nowrap">
        {count(item.input_tokens + item.output_tokens)}
        <small>
          {count(item.input_tokens)} вход / {count(item.output_tokens)} выход
        </small>
      </td>
      <td className="mono wv-nowrap">{money(item.charged_rub, 4)} ₽</td>
      <td>
        <Status status={item.status} />
      </td>
    </tr>
  );
}

function KeysView({ data, onRefresh, onNotice }: WorkspaceProps) {
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [rawKey, setRawKey] = useState("");
  const [error, setError] = useState("");
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [revoking, setRevoking] = useState<number | null>(null);

  async function createKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreating(true);
    setError("");
    try {
      const result = await api<{ raw_key: string }>("/cabinet-api/keys", {
        method: "POST",
        body: JSON.stringify({ name: name.trim() }),
      });
      setRawKey(result.raw_key);
      setName("");
      setShowForm(false);
      await onRefresh().catch(() =>
        onNotice(
          "Ключ создан. Обновите страницу, чтобы обновить список ключей.",
        ),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setCreating(false);
    }
  }

  async function revokeKey(id: number) {
    setRevoking(id);
    setError("");
    try {
      await api(`/cabinet-api/keys/${id}`, { method: "DELETE" });
      setConfirmId(null);
      onNotice("Ключ отозван. Новые запросы с ним недоступны.");
      await onRefresh().catch(() =>
        onNotice("Ключ отозван. Обновите страницу, чтобы обновить список."),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRevoking(null);
    }
  }

  return (
    <div className="wv-page">
      <ViewHeading
        title="Ваш доступ к API"
        description="Подключайте свои приложения. Управляйте каждым ключом отдельно."
        action={
          <button
            className="button button-primary"
            disabled={creating || Boolean(rawKey)}
            onClick={() => setShowForm((value) => !value)}
          >
            <Plus size={17} />
            Создать ключ
          </button>
        }
      />
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {rawKey && (
        <div className="wv-key-reveal" role="status">
          <div className="wv-reveal-heading">
            <CheckCheck size={21} />
            <div>
              <h3>Ключ создан. Сохраните его сейчас.</h3>
              <p>
                Полный ключ отображается только один раз. Храните его в
                менеджере секретов.
              </p>
            </div>
          </div>
          <div className="wv-secret-row">
            <code>{rawKey}</code>
            <button
              className="button button-secondary"
              onClick={() => void copyText(rawKey, onNotice)}
            >
              <Copy size={16} />
              Копировать
            </button>
          </div>
          <button className="wv-text-button" onClick={() => setRawKey("")}>
            <Check size={15} />Я сохранил ключ
          </button>
        </div>
      )}
      {showForm && (
        <form
          className="wv-inline-form"
          onSubmit={(event) => void createKey(event)}
        >
          <label htmlFor="key-name">
            Название ключа
            <span className="muted">Поможет отличать проекты и окружения.</span>
          </label>
          <div>
            <input
              className="field"
              id="key-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Например, рабочий проект"
              maxLength={120}
              autoFocus
              disabled={creating}
            />
            <button
              className="button button-primary"
              type="submit"
              disabled={creating}
            >
              {creating ? (
                <LoaderCircle size={16} className="wv-spinning" />
              ) : (
                <KeyRound size={16} />
              )}
              Создать
            </button>
            <button
              className="icon-button"
              type="button"
              disabled={creating}
              onClick={() => setShowForm(false)}
              aria-label="Отменить создание ключа"
            >
              <X size={19} />
            </button>
          </div>
        </form>
      )}
      <div className="wv-key-list">
        {data.api_keys.map((key) => (
          <article className="wv-key-row" key={key.id}>
            <div className="wv-key-symbol">
              <KeyRound size={21} />
            </div>
            <div className="wv-key-details">
              <h3>{key.name || "Без названия"}</h3>
              <code>{key.prefix}</code>
              <span>Создан {dateLabel(key.created_at)}</span>
            </div>
            <div className="wv-key-limits">
              <span>Лимит в день</span>
              <strong>
                {key.daily_limit_rub === null
                  ? "Без ограничения"
                  : `${money(key.daily_limit_rub)} ₽`}
              </strong>
            </div>
            {confirmId === key.id ? (
              <div className="wv-revoke-confirm">
                <p>Отозвать этот ключ?</p>
                <div>
                  <button
                    className="button wv-danger-button"
                    disabled={revoking !== null}
                    onClick={() => void revokeKey(key.id)}
                  >
                    {revoking === key.id ? (
                      <LoaderCircle size={15} className="wv-spinning" />
                    ) : (
                      <Trash2 size={15} />
                    )}
                    Отозвать
                  </button>
                  <button
                    className="button button-secondary"
                    disabled={revoking !== null}
                    onClick={() => setConfirmId(null)}
                  >
                    Отмена
                  </button>
                </div>
              </div>
            ) : (
              <button
                className="icon-button wv-revoke-button"
                disabled={revoking !== null}
                onClick={() => setConfirmId(key.id)}
                aria-label={`Отозвать ключ ${key.name || key.prefix}`}
                title="Отозвать ключ"
              >
                <Trash2 size={18} />
              </button>
            )}
          </article>
        ))}
      </div>
      {data.api_keys.length === 0 && (
        <EmptyState
          icon={<KeyRound size={27} />}
          title="Подключите свой первый проект"
        >
          Создайте ключ и используйте его в приложении с OpenAI-совместимым API.
        </EmptyState>
      )}
      <div className="wv-inline-note">
        <ShieldCheck size={18} />
        <p>
          Ключ открывает доступ к вашему балансу. Не публикуйте его в коде и не
          передавайте в браузер. Для каждого проекта создавайте отдельный ключ.
        </p>
      </div>
    </div>
  );
}

function WalletView({ data, onRefresh, onNotice }: WorkspaceProps) {
  const [amount, setAmount] = useState("1000");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const [error, setError] = useState("");

  async function requestTopup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setSuccess(false);
    setError("");
    try {
      await api("/cabinet-api/topups", {
        method: "POST",
        body: JSON.stringify({ amount_rub: amount, note: note.trim() }),
      });
      setSuccess(true);
      setNote("");
      await onRefresh().catch(() =>
        onNotice(
          "Заявка отправлена. Обновите страницу, чтобы увидеть её в истории.",
        ),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="wv-page">
      <ViewHeading
        title="Баланс без сюрпризов"
        description="Пополняйте кошелёк и оплачивайте только использованные запросы."
      />
      <div className="wv-wallet-layout">
        <section className="wv-wallet-balance">
          <div className="wv-wallet-label">
            <Wallet size={21} />
            Ваш кошелёк
          </div>
          <p className="wv-wallet-amount">
            {money(data.wallet.available_rub)} <span>₽</span>
          </p>
          <p className="muted">Доступно для запросов</p>
          <div className="wv-balance-details">
            <div>
              <span>Общий баланс</span>
              <strong>{money(data.wallet.balance_rub)} ₽</strong>
            </div>
            <div>
              <span>В резерве</span>
              <strong>{money(data.wallet.reserved_rub)} ₽</strong>
            </div>
            <div>
              <span>Расход за месяц</span>
              <strong>{money(data.wallet.spent_month_rub)} ₽</strong>
            </div>
          </div>
          <div className="wv-wallet-footnote">
            <ShieldCheck size={17} />
            <p>
              Резерв удерживается на время запроса. После завершения списывается
              фактическая стоимость.
            </p>
          </div>
        </section>
        <section className="wv-topup-panel">
          <h2>Пополнить баланс</h2>
          <p>
            Создайте заявку — администратор подтвердит зачисление после проверки
            оплаты.
          </p>
          {data.customer.is_child ? (
            <div className="wv-inline-note">
              <ShieldCheck size={20} />
              <p>
                Вы используете общий кошелёк. Пополнить его может владелец
                основного аккаунта.
              </p>
            </div>
          ) : (
            <form onSubmit={(event) => void requestTopup(event)}>
              <label htmlFor="topup-amount">Сумма, ₽</label>
              <input
                className="field wv-amount-input"
                type="number"
                id="topup-amount"
                min="0.01"
                max="999999999999.99"
                step="0.01"
                inputMode="decimal"
                required
                value={amount}
                onChange={(event) => {
                  setAmount(event.target.value);
                  setSuccess(false);
                }}
                disabled={submitting}
              />
              <div className="wv-amount-options">
                {[500, 1000, 3000, 5000].map((value) => (
                  <button
                    type="button"
                    key={value}
                    className={Number(amount) === value ? "is-selected" : ""}
                    aria-pressed={Number(amount) === value}
                    disabled={submitting}
                    onClick={() => {
                      setAmount(String(value));
                      setSuccess(false);
                    }}
                  >
                    {count(value)} ₽
                  </button>
                ))}
              </div>
              <label htmlFor="topup-note">
                Комментарий <span className="muted">· необязательно</span>
              </label>
              <input
                className="field"
                id="topup-note"
                placeholder="Например, номер платежа"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                maxLength={1000}
                disabled={submitting}
              />
              {error && <ErrorNotice>{error}</ErrorNotice>}
              {success && (
                <div className="wv-success" role="status">
                  <CheckCheck size={19} />
                  <span>
                    Заявка отправлена на проверку. Баланс обновится после
                    подтверждения.
                  </span>
                </div>
              )}
              <button
                className="button button-primary wv-topup-submit"
                disabled={
                  submitting ||
                  success ||
                  !Number.isFinite(Number(amount)) ||
                  Number(amount) <= 0
                }
                type="submit"
              >
                {submitting ? (
                  <LoaderCircle size={17} className="wv-spinning" />
                ) : (
                  <ArrowUpRight size={18} />
                )}
                {success ? "Заявка отправлена" : "Отправить заявку"}
              </button>
              <p className="wv-form-hint">
                Отправка заявки сама по себе не списывает и не зачисляет деньги.
              </p>
            </form>
          )}
        </section>
      </div>
      <div className="section-heading wv-history-heading">
        <h2>История пополнений</h2>
        <span className="muted">Последние 20 заявок</span>
      </div>
      <div className="wv-topup-history">
        {data.topups.map((topup) => (
          <div className="wv-topup-row" key={topup.id}>
            <span
              className={`wv-transfer-icon ${topup.status === "confirmed" ? "is-confirmed" : ""}`}
            >
              <ArrowDownLeft size={20} />
            </span>
            <div>
              <strong>Пополнение #{topup.id}</strong>
              <small>
                {dateLabel(topup.created_at)}
                {topup.note ? ` · ${topup.note}` : ""}
              </small>
            </div>
            <span className="wv-topup-value mono">
              {money(topup.amount_rub)} ₽
            </span>
            <Status status={topup.status} />
          </div>
        ))}
        {data.topups.length === 0 && (
          <EmptyState
            icon={<Wallet size={25} />}
            title="Здесь будет история пополнений"
          >
            Отправьте первую заявку. Её статус появится в этом разделе.
          </EmptyState>
        )}
      </div>
    </div>
  );
}

function SettingsView({ data, onNavigate, onNotice }: WorkspaceProps) {
  const snippet = `from openai import OpenAI\nimport os\n\nclient = OpenAI(\n    api_key=os.environ["FLAWLESS_API_KEY"],\n    base_url=os.environ["FLAWLESS_API_BASE_URL"],\n)\n\nresponse = client.chat.completions.create(\n    model="${data.models[0] || "MODEL_ALIAS"}",\n    messages=[{"role": "user", "content": "Привет!"}],\n)\nprint(response.choices[0].message.content)`;
  return (
    <div className="wv-page">
      <ViewHeading
        title="Ваше пространство"
        description="Данные аккаунта, лимиты и подключение приложений."
      />
      <div className="wv-settings-layout">
        <section className="wv-settings-section">
          <h2>Профиль</h2>
          <div className="wv-profile-heading">
            <span className="wv-profile-avatar">
              {data.customer.name.slice(0, 1).toLocaleUpperCase() || "F"}
            </span>
            <div>
              <strong>{data.customer.name || "Пользователь"}</strong>
              <span>
                {data.customer.is_child ? "Детский аккаунт" : "Личный аккаунт"}
              </span>
            </div>
          </div>
          <dl className="wv-details-list">
            <div>
              <dt>Email</dt>
              <dd>{data.customer.email}</dd>
            </div>
            <div>
              <dt>ID аккаунта</dt>
              <dd className="mono">
                FL-{String(data.customer.id).padStart(5, "0")}
              </dd>
            </div>
            <div>
              <dt>API-ключи</dt>
              <dd>
                <button
                  className="wv-text-button"
                  onClick={() => onNavigate("keys")}
                >
                  {data.api_keys.length} активных
                  <ArrowRight size={14} />
                </button>
              </dd>
            </div>
          </dl>
          <p className="wv-settings-hint">
            Для изменения данных аккаунта обратитесь к администратору сервиса.
          </p>
          <h2 className="wv-settings-subheading">Лимиты расходов</h2>
          <dl className="wv-details-list">
            <div>
              <dt>В день</dt>
              <dd>
                {data.wallet.daily_limit_rub === null
                  ? "Не установлен"
                  : `${money(data.wallet.daily_limit_rub)} ₽`}
              </dd>
            </div>
            <div>
              <dt>В месяц</dt>
              <dd>
                {data.wallet.monthly_limit_rub === null
                  ? "Не установлен"
                  : `${money(data.wallet.monthly_limit_rub)} ₽`}
              </dd>
            </div>
          </dl>
          <p className="wv-settings-hint">
            Лимиты задаёт администратор. При достижении лимита новые запросы
            приостанавливаются.
          </p>
        </section>
        <section className="wv-settings-section wv-integration-section">
          <div className="wv-integration-heading">
            <Code2 size={22} />
            <h2>Подключение к API</h2>
          </div>
          <p>
            FLAWLESS поддерживает OpenAI-совместимые клиенты. Создайте ключ и
            передайте настройки через переменные окружения.
          </p>
          <div className="wv-code-block">
            <div>
              <span>Python</span>
              <button
                className="icon-button"
                aria-label="Скопировать пример Python"
                onClick={() => void copyText(snippet, onNotice)}
              >
                <Copy size={15} />
              </button>
            </div>
            <pre>
              <code>{snippet}</code>
            </pre>
          </div>
          <p className="wv-settings-hint">
            <code>FLAWLESS_API_BASE_URL</code> — адрес API вашего сервера с
            окончанием <code>/v1</code>. Адрес можно уточнить у администратора.
          </p>
          <button
            className="button button-secondary"
            onClick={() => onNavigate("keys")}
          >
            <KeyRound size={16} />
            Управление ключами
            <ArrowUpRight size={16} />
          </button>
        </section>
      </div>
    </div>
  );
}
