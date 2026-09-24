import { expect, test } from "@playwright/test";

const prefix = "/api/backend";
const credentials = {
  email: "demo@flawless.local",
  password: "Neon-Demo-2026!",
};

test("cabinet requires a session", async ({ request }) => {
  const response = await request.get(`${prefix}/cabinet-api/dashboard`);
  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");
});

test("invalid login stays unauthorized", async ({ request }) => {
  const response = await request.post(`${prefix}/cabinet-api/login`, {
    data: { ...credentials, password: "incorrect" },
  });
  expect(response.status()).toBe(401);
});

test("browser origin login preserves the session and exact money strings", async ({
  request,
  baseURL,
}) => {
  const login = await request.post(`${prefix}/cabinet-api/login`, {
    headers: { Origin: baseURL! },
    data: credentials,
  });
  expect(login.status()).toBe(200);
  expect(login.headers()["set-cookie"]).toContain("httponly");
  const response = await request.get(`${prefix}/cabinet-api/dashboard`);
  expect(response.status()).toBe(200);
  expect(response.headers()["set-cookie"]).toBeUndefined();
  const body = await response.json();
  expect(body.customer.email).toBe(credentials.email);
  expect(typeof body.wallet.balance_rub).toBe("string");
  expect(body.models.length).toBeGreaterThan(0);
  expect(body.daily_usage).toHaveLength(7);
  expect(JSON.stringify(body)).not.toMatch(
    /password_hash|key_hash|session_secret|raw_key/,
  );
});

test("logout clears the browser session", async ({ request }) => {
  expect(
    (
      await request.post(`${prefix}/cabinet-api/login`, { data: credentials })
    ).status(),
  ).toBe(200);
  expect((await request.post(`${prefix}/cabinet-api/logout`)).status()).toBe(
    200,
  );
  expect((await request.get(`${prefix}/cabinet-api/dashboard`)).status()).toBe(
    401,
  );
});

test("a foreign origin cannot mutate the account", async ({ request }) => {
  const response = await request.post(`${prefix}/cabinet-api/login`, {
    headers: { Origin: "https://untrusted.example" },
    data: credentials,
  });
  expect(response.status()).toBe(403);
});

test("a foreign referer cannot bypass origin validation", async ({
  request,
}) => {
  const response = await request.post(`${prefix}/cabinet-api/login`, {
    headers: { Referer: "https://untrusted.example/attack" },
    data: credentials,
  });
  expect(response.status()).toBe(403);
});

test("the gateway does not expose arbitrary backend paths", async ({
  request,
}) => {
  expect((await request.get(`${prefix}/admin/customers`)).status()).toBe(404);
  expect((await request.get(`${prefix}/cabinet-api/login`)).status()).toBe(404);
});

test("large request bodies are rejected before reaching Python", async ({
  request,
}) => {
  const response = await request.post(`${prefix}/cabinet-api/login`, {
    data: Buffer.alloc(8 * 1024 * 1024 + 1, "x"),
  });
  expect(response.status()).toBe(413);
});
