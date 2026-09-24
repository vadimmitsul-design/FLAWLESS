import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 8 * 1024 * 1024;
const UPSTREAM_TIMEOUT_MS = 140_000;
type RouteContext = { params: Promise<{ path: string[] }> };

const routes: ReadonlyArray<{ method: string; path: RegExp }> = [
  { method: "GET", path: /^cabinet-api\/(session|dashboard)$/ },
  { method: "GET", path: /^cabinet-api\/conversations\/[1-9]\d*$/ },
  { method: "POST", path: /^cabinet-api\/(login|logout|keys|topups)$/ },
  { method: "DELETE", path: /^cabinet-api\/keys\/[1-9]\d*$/ },
  { method: "POST", path: /^chat\/send$/ },
];

function failure(detail: string, status: number): NextResponse {
  return NextResponse.json(
    { detail },
    {
      status,
      headers: { "Cache-Control": "no-store" },
    },
  );
}

class BodyTooLarge extends Error {}

function frontendOrigin(request: NextRequest): string {
  const configured = process.env.FLAWLESS_FRONTEND_ORIGIN;
  const host = request.headers.get("host");
  if (!configured && (!host || /[\\/#?@\s]/.test(host))) {
    throw new Error("Invalid request host");
  }
  // Next may normalize request.nextUrl.hostname to "localhost" in development.
  // Host retains the actual browser authority; forwarded headers are not trusted.
  const origin = new URL(configured || `${request.nextUrl.protocol}//${host}`);
  if (
    !["http:", "https:"].includes(origin.protocol) ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash
  ) {
    throw new Error("Invalid frontend origin");
  }
  return origin.origin;
}

async function boundedBody(
  request: NextRequest,
): Promise<ArrayBuffer | undefined> {
  const declaredLength = request.headers.get("content-length");
  if (declaredLength !== null && Number(declaredLength) > MAX_BODY_BYTES) {
    throw new BodyTooLarge();
  }
  if (!request.body) return undefined;

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new BodyTooLarge();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer;
}

async function proxy(
  request: NextRequest,
  context: RouteContext,
): Promise<Response> {
  const { path } = await context.params;
  const pathname = path.join("/");
  if (
    !routes.some(
      (route) => route.method === request.method && route.path.test(pathname),
    )
  ) {
    return failure("Маршрут не найден", 404);
  }

  if (request.method !== "GET") {
    const claimedOrigin =
      request.headers.get("origin") || request.headers.get("referer");
    try {
      if (
        request.headers.get("sec-fetch-site") === "cross-site" ||
        (claimedOrigin &&
          new URL(claimedOrigin).origin !== frontendOrigin(request))
      ) {
        return failure("Межсайтовый запрос заблокирован", 403);
      }
    } catch {
      return failure("Недопустимый источник запроса", 403);
    }
  }

  try {
    const backend = new URL(
      process.env.FLAWLESS_BACKEND_URL || "http://127.0.0.1:8001",
    );
    if (
      !["http:", "https:"].includes(backend.protocol) ||
      backend.username ||
      backend.password
    ) {
      return failure("Сервис временно недоступен", 503);
    }
    const target = new URL(`/${pathname}`, backend);
    const headers = new Headers();
    for (const name of ["cookie", "content-type", "accept"]) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    // Validate the browser's origin above before using the backend's own origin.
    if (request.method !== "GET") headers.set("origin", backend.origin);

    const bytes =
      request.method === "GET" ? undefined : await boundedBody(request);
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: bytes,
      redirect: "error",
      cache: "no-store",
      signal: AbortSignal.any([
        request.signal,
        AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      ]),
    });
    const responseHeaders = new Headers({
      "Cache-Control": "no-store",
      Vary: "Cookie",
    });
    const contentType = upstream.headers.get("content-type");
    if (contentType) responseHeaders.set("content-type", contentType);
    // Starlette refreshes its signed cookie on every response. A late chat or
    // dashboard response must not restore the browser cookie after logout.
    if (pathname === "cabinet-api/login" || pathname === "cabinet-api/logout") {
      for (const cookie of upstream.headers.getSetCookie()) {
        responseHeaders.append("set-cookie", cookie);
      }
    }
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch (error) {
    if (error instanceof BodyTooLarge)
      return failure("Размер запроса превышает 8 МБ", 413);
    return failure("Не удалось связаться с сервером. Попробуйте ещё раз.", 503);
  }
}

export { proxy as GET, proxy as POST, proxy as DELETE };
