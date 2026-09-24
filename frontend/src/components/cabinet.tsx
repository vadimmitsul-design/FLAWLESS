"use client";

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react";
import {
  ArrowDownLeft,
  ArrowRight,
  ArrowUpRight,
  AudioLines,
  BarChart3,
  Check,
  ChevronRight,
  CircleHelp,
  Code2,
  Command,
  Cpu,
  CreditCard,
  FileText,
  KeyRound,
  LayoutDashboard,
  Lightbulb,
  LoaderCircle,
  LogOut,
  Menu,
  MessageSquare,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Wallet,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";
import {
  api,
  ApiError,
  count,
  dateLabel,
  modelInfo,
  money,
  SESSION_EXPIRED_EVENT,
} from "@/lib/api";
import type { Dashboard, View } from "@/lib/types";
import { NeuralCore } from "./neural-core";
import { WorkspaceView } from "./workspace-views";

const navigation: {
  id: View;
  label: string;
  icon: LucideIcon;
  group: number;
}[] = [
  { id: "overview", label: "Обзор", icon: LayoutDashboard, group: 0 },
  { id: "chat", label: "AI-чат", icon: MessageSquare, group: 0 },
  { id: "models", label: "Модели", icon: Cpu, group: 0 },
  { id: "usage", label: "Использование", icon: BarChart3, group: 1 },
  { id: "wallet", label: "Кошелёк", icon: Wallet, group: 1 },
  { id: "keys", label: "API-ключи", icon: KeyRound, group: 1 },
];
const viewLabels: Record<View, string> = {
  overview: "Обзор",
  chat: "AI-чат",
  models: "Модели",
  usage: "Использование",
  wallet: "Кошелёк",
  keys: "API-ключи",
  settings: "Настройки",
};
const validViews = Object.keys(viewLabels) as View[];
const subscribeView = (listener: () => void) => {
  window.addEventListener("hashchange", listener);
  return () => window.removeEventListener("hashchange", listener);
};
const currentView = (): View => {
  const value = window.location.hash.slice(1) as View;
  return validViews.includes(value) ? value : "overview";
};
const suggestions = [
  {
    icon: FileText,
    label: "Написать текст",
    prompt:
      "Помоги написать текст. Сначала уточни у меня тему, аудиторию и желаемый формат.",
  },
  {
    icon: Code2,
    label: "Разобрать код",
    prompt:
      "Помоги разобраться в коде. Я пришлю фрагмент, а ты объясни его работу и предложи улучшения.",
  },
  {
    icon: Lightbulb,
    label: "Найти идею",
    prompt:
      "Давай устроим мозговой штурм. Уточни мою задачу и предложи несколько необычных подходов.",
  },
];

function Brand() {
  return (
    <span className="brand">
      <svg viewBox="0 0 36 40" aria-hidden="true">
        <path
          d="M12 3h24l-4 8H19l-3 6h14l-4 8H12L7 37H0Z"
          fill="currentColor"
        />
      </svg>
      <span>
        FLAWLESS<span className="brand-dot">®</span>
      </span>
    </span>
  );
}

export function Cabinet({ demo }: { demo: boolean }) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const view = useSyncExternalStore(
    subscribeView,
    currentView,
    () => "overview" as View,
  );
  const [mobileNav, setMobileNav] = useState(false);
  const [notice, setNotice] = useState("");
  const [initialModel, setInitialModel] = useState("");
  const [initialPrompt, setInitialPrompt] = useState("");
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [chatInstance, setChatInstance] = useState(0);
  const [search, setSearch] = useState("");
  const searchDialog = useRef<HTMLDialogElement>(null);
  const helpDialog = useRef<HTMLDialogElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const refreshRequest = useRef<AbortController | null>(null);
  const refreshGeneration = useRef(0);
  const sessionClosed = useRef(false);

  const clearSession = useCallback(() => {
    sessionClosed.current = true;
    refreshGeneration.current += 1;
    refreshRequest.current?.abort();
    setData(null);
    setLoading(false);
    setLoadError("");
    setNotice("");
    setInitialModel("");
    setInitialPrompt("");
    setConversationId(null);
    setMobileNav(false);
  }, []);

  const refresh = useCallback(() => {
    if (sessionClosed.current) return Promise.resolve();
    const generation = ++refreshGeneration.current;
    refreshRequest.current?.abort();
    const controller = new AbortController();
    refreshRequest.current = controller;
    return api<Dashboard>("/cabinet-api/dashboard", {
      signal: controller.signal,
    })
      .then((result) => {
        if (generation !== refreshGeneration.current || sessionClosed.current)
          return;
        setData(result);
        setLoadError("");
      })
      .catch((error: unknown) => {
        if (
          controller.signal.aborted ||
          generation !== refreshGeneration.current
        )
          return;
        if (error instanceof ApiError && error.status === 401) {
          clearSession();
        } else {
          setLoadError(
            error instanceof Error
              ? error.message
              : "Не удалось загрузить кабинет",
          );
          throw error;
        }
      })
      .finally(() => {
        if (generation === refreshGeneration.current) setLoading(false);
      });
  }, [clearSession]);

  const finishLogin = useCallback(() => {
    sessionClosed.current = false;
    return refresh();
  }, [refresh]);

  useEffect(() => {
    window.addEventListener(SESSION_EXPIRED_EVENT, clearSession);
    // The banner renders load errors; mutation callers can handle refresh failure separately.
    void refresh().catch(() => undefined);
    return () => {
      window.removeEventListener(SESSION_EXPIRED_EVENT, clearSession);
      refreshGeneration.current += 1;
      refreshRequest.current?.abort();
    };
  }, [clearSession, refresh]);
  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(""), 4500);
    return () => window.clearTimeout(timeout);
  }, [notice]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchDialog.current?.showModal();
        searchInput.current?.focus();
      }
      if (event.key === "Escape") setMobileNav(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const navigate = (next: View) => {
    window.history.pushState(null, "", `#${next}`);
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    setMobileNav(false);
    searchDialog.current?.close();
    setSearch("");
  };
  const startChat = (
    model = data?.models[0] || "",
    prompt = "",
    id?: number,
  ) => {
    setInitialModel(model);
    setInitialPrompt(prompt);
    setConversationId(id ?? null);
    setChatInstance((value) => value + 1);
    navigate("chat");
  };
  const logout = async () => {
    if (sessionClosed.current) return;
    sessionClosed.current = true;
    const generation = ++refreshGeneration.current;
    refreshRequest.current?.abort();
    try {
      await api("/cabinet-api/logout", { method: "POST" });
      clearSession();
      navigate("overview");
    } catch (error) {
      if (generation === refreshGeneration.current)
        sessionClosed.current = false;
      setNotice(error instanceof Error ? error.message : "Не удалось выйти");
    }
  };

  if (loading)
    return (
      <main className="boot-screen">
        <Brand />
        <div className="boot-track" />
        <p>Подключаем твоё пространство</p>
      </main>
    );
  if (!data)
    return (
      <Login demo={demo} onLogin={finishLogin} connectionError={loadError} />
    );

  const firstName = data.customer.name.trim().split(" ")[0] || "друг";
  const initials =
    data.customer.name
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map((word) => word[0])
      .join("")
      .toUpperCase() || "F";
  const currentTitle = viewLabels[view];
  const searchItems = [
    ...navigation.map((item) => ({
      key: item.id,
      label: item.label,
      caption: "Раздел",
      action: () => navigate(item.id),
    })),
    ...data.models.map((model) => ({
      key: model,
      label: modelInfo(model).name,
      caption: "Начать чат",
      action: () => startChat(model),
    })),
    ...data.conversations.map((item) => ({
      key: `chat-${item.id}`,
      label: item.title,
      caption: "Диалог",
      action: () => startChat(item.model, "", item.id),
    })),
  ].filter((item) =>
    `${item.label} ${item.caption}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );

  return (
    <div className="workspace">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main-content")?.focus();
        }}
      >
        Перейти к содержимому
      </a>
      {mobileNav && (
        <button
          className="nav-backdrop"
          aria-label="Закрыть навигацию"
          onClick={() => setMobileNav(false)}
        />
      )}
      <aside
        className={`sidebar ${mobileNav ? "is-open" : ""}`}
        aria-label="Основная навигация"
      >
        <button
          className="brand-button"
          aria-label="FLAWLESS — обзор"
          onClick={() => navigate("overview")}
        >
          <Brand />
        </button>
        <div className="workspace-selector">
          <span className="workspace-emblem">
            <AudioLines size={20} />
          </span>
          <span>
            <strong>Личное пространство</strong>
            <small>
              {demo ? "Демонстрационный аккаунт" : "Твой AI-кабинет"}
            </small>
          </span>
          <ChevronRight size={15} />
        </div>
        <nav className="nav-list">
          {navigation.map((item, index) => (
            <div key={item.id}>
              {index === 3 && (
                <div className="nav-separator">
                  <span>Управление</span>
                </div>
              )}
              <button
                className={`nav-item ${view === item.id ? "active" : ""}`}
                onClick={() => navigate(item.id)}
                aria-current={view === item.id ? "page" : undefined}
              >
                <item.icon size={19} />
                <span>{item.label}</span>
                {item.id === "chat" && <span className="nav-chat-mark">↗</span>}
                {item.id === "models" && (
                  <span className="nav-counter">{data.models.length}</span>
                )}
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <Zap size={17} />
            <p>
              От идеи до результата.
              <br />
              <strong>В одном пространстве.</strong>
            </p>
            <span className="tiny-star">✳</span>
          </div>
          <button
            className={`nav-item ${view === "settings" ? "active" : ""}`}
            onClick={() => navigate("settings")}
          >
            <Settings2 size={19} />
            <span>Настройки</span>
          </button>
          <button
            className="nav-item"
            onClick={() => helpDialog.current?.showModal()}
          >
            <CircleHelp size={19} />
            <span>Помощь</span>
            <ArrowUpRight size={15} />
          </button>
          <div className="sidebar-user">
            <button
              onClick={() => navigate("settings")}
              className="user-profile"
            >
              <span className="avatar">{initials}</span>
              <span>
                <strong>{data.customer.name}</strong>
                <small>Личный аккаунт</small>
              </span>
            </button>
            <button
              className="icon-button logout-button"
              aria-label="Выйти из аккаунта"
              onClick={() => void logout()}
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>

      <div className="workspace-main">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="Открыть навигацию"
              aria-expanded={mobileNav}
              onClick={() => setMobileNav(true)}
            >
              <Menu size={21} />
            </button>
            <span className="breadcrumb-root">Рабочее пространство</span>
            <ChevronRight size={14} />
            <strong>{currentTitle}</strong>
          </div>
          <div className="topbar-actions">
            <button
              className="search-trigger"
              aria-label="Быстрый поиск"
              onClick={() => {
                searchDialog.current?.showModal();
                searchInput.current?.focus();
              }}
            >
              <Search size={16} />
              <span>Быстрый поиск</span>
              <kbd>⌘ K</kbd>
            </button>
            <span className="topbar-divider" />
            <button
              className="avatar avatar-small"
              onClick={() => navigate("settings")}
              aria-label="Мой аккаунт"
            >
              {initials}
            </button>
          </div>
        </header>
        <main id="main-content" className="content" tabIndex={-1}>
          {loadError && (
            <div className="inline-error" role="alert">
              {loadError}
              <button onClick={() => void refresh().catch(() => undefined)}>
                Обновить
              </button>
            </div>
          )}
          {view === "overview" ? (
            <>
              <div className="page-heading">
                <div>
                  <div className="greeting-line">
                    <h1>
                      Привет, {firstName}
                      <span className="greeting-dot">.</span>
                    </h1>
                    <span className="greeting-spark">✳</span>
                  </div>
                  <p>Хороший день, чтобы создать что-то новое.</p>
                </div>
                <div className="space-status">
                  <span className="status-light" />
                  {demo ? "ДЕМО-ПРОСТРАНСТВО" : "ЛИЧНОЕ ПРОСТРАНСТВО"}
                  <span className="status-date">
                    {new Date().toLocaleDateString("ru-RU", {
                      day: "2-digit",
                      month: "short",
                      year: "numeric",
                    })}
                  </span>
                </div>
              </div>
              <div className="hero-row">
                <section
                  className="hero-panel"
                  aria-label="Быстрый запуск чата"
                >
                  <div className="hero-copy">
                    <div className="hero-badge">
                      <span className="hero-badge-dot" />
                      ТВОЙ AI. ТВОИ ПРАВИЛА.
                    </div>
                    <h2>
                      Мысли шире.
                      <br />
                      <span>Создавай быстрее.</span>
                    </h2>
                    <p>
                      Сильные модели. Одно пространство.
                      <br />
                      Всё, чтобы воплотить твою следующую идею.
                    </p>
                    <button
                      className="button button-primary hero-cta"
                      onClick={() => startChat()}
                    >
                      <Sparkles size={17} />
                      Начать новый чат
                      <ArrowUpRight size={18} />
                    </button>
                    <div className="hero-models">
                      <span className="mini-model mint">◎</span>
                      <span className="mini-model peach">✳</span>
                      <span className="mini-model blue">✦</span>
                      <span>
                        {data.models.length} модели в твоём распоряжении
                      </span>
                    </div>
                  </div>
                  <NeuralCore />
                  <span className="hero-corner" />
                </section>
                <section className="balance-panel">
                  <div className="panel-topline">
                    <span>
                      <Wallet size={17} />
                      Твой баланс
                    </span>
                    <span className="mono currency-label">RUB</span>
                  </div>
                  <div className="balance-amount">
                    {money(data.wallet.available_rub).split(",")[0]}
                    <span>
                      ,{money(data.wallet.available_rub).split(",")[1]} <i>₽</i>
                    </span>
                  </div>
                  <span className="balance-caption">
                    Доступно для новых идей
                  </span>
                  <div className="balance-detail">
                    <span>В резерве</span>
                    <span className="mono">
                      {money(data.wallet.reserved_rub)} ₽
                    </span>
                  </div>
                  <button
                    className="button button-light"
                    onClick={() => navigate("wallet")}
                  >
                    <Plus size={18} />
                    {data.customer.is_child
                      ? "Посмотреть кошелёк"
                      : "Пополнить баланс"}
                    <ArrowUpRight size={17} />
                  </button>
                  <div className="wallet-note">
                    <ShieldCheck size={15} />
                    <span>Оплата только за использование</span>
                  </div>
                </section>
              </div>
              <div className="quick-start">
                <span>С чего начнём?</span>
                {suggestions.map((suggestion) => (
                  <button
                    key={suggestion.label}
                    onClick={() => startChat(undefined, suggestion.prompt)}
                  >
                    <suggestion.icon size={15} />
                    {suggestion.label}
                    <ArrowUpRight size={14} />
                  </button>
                ))}
              </div>
              <section
                className="metrics-strip"
                aria-label="Использование за текущий месяц"
              >
                <div className="metric">
                  <span className="metric-symbol">
                    <Zap size={19} />
                  </span>
                  <div>
                    <span>Запросов за месяц</span>
                    <strong>
                      {count(data.stats.requests_month)}
                      <small>запросов</small>
                    </strong>
                  </div>
                  <svg
                    className="mini-spark"
                    viewBox="0 0 76 26"
                    aria-hidden="true"
                  >
                    <path
                      d={sparkPath(
                        data.daily_usage.map((day) => day.requests),
                        76,
                        24,
                      )}
                    />
                  </svg>
                </div>
                <div className="metric">
                  <span className="metric-symbol violet-symbol">
                    <AudioLines size={19} />
                  </span>
                  <div>
                    <span>Токенов обработано</span>
                    <strong>
                      {count(data.stats.tokens_month)}
                      <small>за месяц</small>
                    </strong>
                  </div>
                </div>
                <div className="metric">
                  <span className="metric-symbol">
                    <CreditCard size={19} />
                  </span>
                  <div>
                    <span>Расход за месяц</span>
                    <strong>
                      {money(data.wallet.spent_month_rub)}
                      <small>₽</small>
                    </strong>
                  </div>
                  <button
                    className="icon-button"
                    aria-label="Посмотреть расходы"
                    onClick={() => navigate("usage")}
                  >
                    <ArrowUpRight size={19} />
                  </button>
                </div>
              </section>
              <div className="dashboard-bottom">
                <section className="activity-panel">
                  <div className="section-heading">
                    <h2>
                      Твоя активность
                      <span className="section-dot" />
                    </h2>
                    <span className="period-label">Последние 7 дней</span>
                  </div>
                  <ActivityChart data={data} />
                  <div className="chart-footer">
                    <span>
                      <span className="chart-legend" />
                      Расход, ₽
                    </span>
                    <button
                      className="text-button"
                      onClick={() => navigate("usage")}
                    >
                      Вся статистика
                      <ArrowUpRight size={15} />
                    </button>
                  </div>
                </section>
                <section className="recent-panel">
                  <div className="section-heading">
                    <h2>Продолжить диалог</h2>
                    <button
                      className="icon-button"
                      aria-label="Новый диалог"
                      onClick={() => startChat()}
                    >
                      <Plus size={18} />
                    </button>
                  </div>
                  {data.conversations.length ? (
                    <div className="recent-list">
                      {data.conversations.slice(0, 3).map((conversation) => (
                        <button
                          className="recent-item"
                          key={conversation.id}
                          onClick={() =>
                            startChat(conversation.model, "", conversation.id)
                          }
                        >
                          <span
                            className={`model-mark ${modelInfo(conversation.model).color}`}
                          >
                            {modelInfo(conversation.model).mark}
                          </span>
                          <span>
                            <strong>{conversation.title}</strong>
                            <small>
                              {modelInfo(conversation.model).name}
                              <span>·</span>
                              {dateLabel(conversation.updated_at)}
                            </small>
                          </span>
                          <ChevronRight size={17} />
                        </button>
                      ))}
                    </div>
                  ) : (
                    <div className="recent-empty">
                      <MessageSquare size={26} />
                      <p>Здесь будут твои диалоги.</p>
                      <button
                        className="text-button"
                        onClick={() => startChat()}
                      >
                        Создать первый
                        <ArrowRight size={15} />
                      </button>
                    </div>
                  )}
                  <button
                    className="all-chats"
                    onClick={() => navigate("chat")}
                  >
                    Открыть AI-чат
                    <ArrowRight size={16} />
                  </button>
                </section>
              </div>
              <section className="models-section">
                <div className="section-heading">
                  <h2>
                    Выбери свой интеллект
                    <span className="count-badge">{data.models.length}</span>
                  </h2>
                  <button
                    className="text-button"
                    onClick={() => navigate("models")}
                  >
                    Все модели
                    <ArrowUpRight size={15} />
                  </button>
                </div>
                <div className="model-ribbon">
                  {data.models.map((model) => {
                    const info = modelInfo(model);
                    return (
                      <button
                        className="model-ribbon-item"
                        key={model}
                        onClick={() => startChat(model)}
                      >
                        <span className={`model-mark ${info.color}`}>
                          {info.mark}
                        </span>
                        <span>
                          <strong>{info.name}</strong>
                          <small>{info.tag}</small>
                        </span>
                        <ArrowUpRight size={16} />
                      </button>
                    );
                  })}
                </div>
              </section>
              <footer className="content-footer">
                <span>
                  FLAWLESS<span className="footer-slash">/</span>Меньше
                  ограничений. Больше возможностей.
                </span>
                <span className="mono">
                  {demo
                    ? "DEMO · ДАННЫЕ ДЛЯ ПОКАЗА"
                    : "YOUR INTELLIGENCE SPACE"}
                </span>
              </footer>
            </>
          ) : (
            <>
              {view === "chat" && <h1 className="sr-only">AI-чат</h1>}
              {demo && (
                <div className="demo-banner">
                  <span className="demo-tag">Демо</span>
                  <span>
                    Данные для показа. История и формы работают; платные
                    AI-модели отключены.
                  </span>
                </div>
              )}
              <WorkspaceView
                key={`${view}-${chatInstance}`}
                view={view}
                data={data}
                onRefresh={refresh}
                onNavigate={navigate}
                initialModel={initialModel || data.models[0] || ""}
                initialPrompt={initialPrompt}
                conversationId={conversationId}
                onNotice={setNotice}
                onStartChat={startChat}
              />
            </>
          )}
        </main>
      </div>
      <dialog
        className="command-dialog"
        ref={searchDialog}
        onClick={(event) => {
          if (event.target === event.currentTarget)
            searchDialog.current?.close();
        }}
      >
        <div className="command-search">
          <Search size={20} />
          <input
            ref={searchInput}
            aria-label="Найти раздел, модель или диалог"
            placeholder="Раздел, модель или диалог…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <button
            className="icon-button"
            onClick={() => searchDialog.current?.close()}
            aria-label="Закрыть поиск"
          >
            <X size={19} />
          </button>
        </div>
        <div className="command-results">
          {searchItems.length ? (
            searchItems.slice(0, 12).map((item) => (
              <button key={item.key} onClick={item.action}>
                <span>{item.label}</span>
                <small>{item.caption}</small>
                <ArrowUpRight size={16} />
              </button>
            ))
          ) : (
            <p className="muted">
              Ничего не найдено. Попробуй другое название.
            </p>
          )}
        </div>
        <div className="command-footer">
          <Command size={13} /> K — открыть поиск<span>Esc — закрыть</span>
        </div>
      </dialog>
      <dialog
        className="help-dialog"
        ref={helpDialog}
        onClick={(event) => {
          if (event.target === event.currentTarget) helpDialog.current?.close();
        }}
      >
        <div className="section-heading">
          <h2>Освойся за минуту</h2>
          <button
            className="icon-button"
            aria-label="Закрыть помощь"
            onClick={() => helpDialog.current?.close()}
          >
            <X size={20} />
          </button>
        </div>
        <dl>
          <dt>Как начать?</dt>
          <dd>
            Открой AI-чат, выбери модель и напиши задачу. Диалоги сохраняются
            автоматически.
          </dd>
          <dt>Как работает баланс?</dt>
          <dd>
            Перед запросом резервируется сумма. После ответа списывается
            фактическая стоимость, остаток резерва освобождается.
          </dd>
          <dt>Как пополнить?</dt>
          <dd>
            Оставь заявку в разделе «Кошелёк». Баланс обновится после
            подтверждения администратором.
          </dd>
          <dt>Как подключить API?</dt>
          <dd>
            Создай ключ в разделе «API-ключи» и сохрани его: полный ключ
            показывается один раз.
          </dd>
        </dl>
        {demo && (
          <p className="demo-help">
            Сейчас открыт демонстрационный аккаунт. Данные вымышлены; платные
            модели в локальном показе не подключены.
          </p>
        )}
      </dialog>
      {notice && (
        <div className="toast" role="status">
          <Check size={18} />
          <span>{notice}</span>
          <button
            className="icon-button"
            aria-label="Скрыть уведомление"
            onClick={() => setNotice("")}
          >
            <X size={16} />
          </button>
        </div>
      )}
    </div>
  );
}

function sparkPath(values: number[], width: number, height: number): string {
  const max = Math.max(...values, 1);
  return values
    .map(
      (value, index) =>
        `${index ? "L" : "M"}${(index * width) / Math.max(values.length - 1, 1)},${height - (value / max) * (height - 3)}`,
    )
    .join(" ");
}

function ActivityChart({ data }: { data: Dashboard }) {
  const values = data.daily_usage.map((day) => Number(day.charged_rub));
  const max = Math.max(...values, 1);
  const ceiling = Math.ceil(max / 10) * 10;
  const width = 560,
    height = 126;
  const points = values
    .map(
      (value, index) =>
        `${(index * width) / Math.max(values.length - 1, 1)},${height - (value / ceiling) * (height - 10)}`,
    )
    .join(" ");
  const total = values.reduce((sum, value) => sum + value, 0);
  return (
    <div className="activity-chart">
      <div className="chart-total">
        <strong>
          {money(total)}
          <span>₽</span>
        </strong>
        <small>за 7 дней</small>
      </div>
      <div className="chart-plot">
        <div className="chart-axis">
          <span>{money(ceiling, 0)}</span>
          <span>{money(ceiling / 2, 0)}</span>
          <span>0</span>
        </div>
        <div className="chart-graphic">
          <svg
            viewBox={`0 0 ${width} ${height + 6}`}
            preserveAspectRatio="none"
            role="img"
            aria-label={`Расход за 7 дней: ${money(total)} рублей`}
          >
            <defs>
              <linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#a694fb" stopOpacity="0.22" />
                <stop offset="100%" stopColor="#a694fb" stopOpacity="0" />
              </linearGradient>
            </defs>
            {[8, height / 2, height].map((y) => (
              <line
                key={y}
                x1="0"
                x2={width}
                y1={y}
                y2={y}
                stroke="#2b293a"
                strokeDasharray="3 5"
              />
            ))}
            <polygon
              points={`0,${height} ${points} ${width},${height}`}
              fill="url(#chart-fill)"
            />
            <polyline
              points={points}
              fill="none"
              stroke="#b49cff"
              strokeWidth="2.5"
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
            {values.map((value, index) => (
              <circle
                key={index}
                cx={(index * width) / Math.max(values.length - 1, 1)}
                cy={height - (value / ceiling) * (height - 10)}
                r="3.5"
                fill="#cbbbff"
              >
                <title>
                  {data.daily_usage[index].date}: {money(value)} ₽
                </title>
              </circle>
            ))}
          </svg>
          <div className="chart-days">
            {data.daily_usage.map((day) => (
              <span key={day.date}>
                {new Date(`${day.date}T12:00:00`).toLocaleDateString("ru-RU", {
                  weekday: "short",
                })}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function Login({
  demo,
  onLogin,
  connectionError,
}: {
  demo: boolean;
  onLogin: () => Promise<void>;
  connectionError: string;
}) {
  const [email, setEmail] = useState(demo ? "demo@flawless.local" : "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      await api("/cabinet-api/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      await onLogin();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Не удалось войти");
    } finally {
      setPending(false);
    }
  };
  return (
    <main className="login-screen">
      <div className="login-art">
        <Brand />
        <div className="login-statement">
          <span className="hero-badge">YOUR INTELLIGENCE SPACE</span>
          <h1>
            Твоя идея.
            <br />
            <span>Следующий уровень.</span>
          </h1>
          <p>
            Модели, которые помогают мыслить шире.
            <br />
            Пространство, которое помогает создавать.
          </p>
        </div>
        <NeuralCore />
        <span className="login-footer">FLAWLESS / СОЗДАВАЙ БУДУЩЕЕ</span>
      </div>
      <section className="login-form-panel">
        <div className="login-form-inner">
          <span className="login-icon">
            <ArrowDownLeft size={25} />
          </span>
          <h2>
            С возвращением<span className="greeting-dot">.</span>
          </h2>
          <p className="muted">Твои идеи уже заждались.</p>
          <form onSubmit={submit}>
            <label className="field-label" htmlFor="login-email">
              Электронная почта
            </label>
            <input
              className="field"
              id="login-email"
              type="email"
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
            <label className="field-label" htmlFor="login-password">
              Пароль
            </label>
            <input
              className="field"
              id="login-password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
            {(error || connectionError) && (
              <p className="form-error" role="alert">
                {error || connectionError}
              </p>
            )}
            <button
              className="button button-primary login-submit"
              disabled={pending}
            >
              {pending ? (
                <LoaderCircle className="spin" size={18} />
              ) : (
                <ArrowRight size={18} />
              )}
              {pending ? "Входим…" : "Войти в пространство"}
            </button>
          </form>
          {demo && (
            <div className="demo-login-note">
              <span className="demo-tag">Локальная демонстрация</span>
              <p>Вымышленные данные. Платные модели отключены.</p>
              <button
                className="text-button"
                onClick={() => {
                  setEmail("demo@flawless.local");
                  setPassword("Neon-Demo-2026!");
                }}
              >
                Заполнить демо-доступ
                <ArrowRight size={15} />
              </button>
            </div>
          )}
          <span className="login-security">
            <ShieldCheck size={14} />
            Защищённая сессия · Ключи остаются на сервере
          </span>
        </div>
      </section>
    </main>
  );
}
