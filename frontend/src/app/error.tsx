"use client";

export default function ErrorPage({ reset }: { reset: () => void }) {
  return (
    <main className="error-screen">
      <span className="brand-word">FLAWLESS</span>
      <h1>Что-то пошло не так</h1>
      <p className="muted">
        Не удалось открыть интерфейс. Попробуйте загрузить его ещё раз.
      </p>
      <button className="button button-primary" onClick={reset}>
        Повторить
      </button>
    </main>
  );
}
