import { afterAll, beforeAll, expect, test } from "bun:test";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";

const repoRoot = join(import.meta.dir, "..");
let baseUrl = "";
let scratch = "";
let databasePath = "";
let serverProcess: Bun.Subprocess | undefined;

async function waitForServer(): Promise<void> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      const response = await fetch(`${baseUrl}/api/threads`);
      if (response.ok) return;
    } catch {
      // The process may still be starting.
    }
    // This is an integration wait for a real child process; fake timers cannot advance server startup.
    await Bun.sleep(20);
  }
  throw new Error("AI Agent Forum test server did not start.");
}
async function post(path: string, payload: Record<string, string>, clientIp = ""): Promise<Response> {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (clientIp) headers["x-airlock-client-ip"] = clientIp;
  return fetch(`${baseUrl}${path}`, { method: "POST", headers, body: JSON.stringify(payload) });
}

beforeAll(async () => {
  await mkdir("/tmp/agents", { recursive: true });
  scratch = await mkdtemp("/tmp/agents/airlock-regression-");
  databasePath = join(scratch, "forum.sqlite");
  serverProcess = Bun.spawn(["bun", "run", "src/index.ts", "serve"], {
    cwd: repoRoot,
    env: { ...process.env, HOST: "127.0.0.1", PORT: "0", BASE_PATH: "/free-open-forum-for-agents-to-collaborate", PUBLIC_URL: "http://127.0.0.1/free-open-forum-for-agents-to-collaborate", DATABASE_PATH: databasePath, TRUST_PROXY: "1" },
    stdout: "pipe",
    stderr: "inherit",
  });
  const output = serverProcess.stdout;
  if (!output || typeof output === "number") throw new Error("Expected server startup output.");
  const reader = output.getReader();
  const decoder = new TextDecoder();
  let startup = "";
  try {
    while (!baseUrl) {
      const chunk = await reader.read();
      if (chunk.done) throw new Error(`Server exited before binding: ${startup}`);
      startup += decoder.decode(chunk.value, { stream: true });
      const address = startup.match(/Airlock listening on (http:\/\/127\.0\.0\.1:\d+\/)/u);
      if (address) baseUrl = `${address[1]}free-open-forum-for-agents-to-collaborate`;
    }
  } finally {
    reader.releaseLock();
  }
  await waitForServer();
});

afterAll(async () => {
  if (serverProcess) {
    serverProcess.kill("SIGTERM");
    await serverProcess.exited;
  }
  await rm(scratch, { recursive: true, force: true });
});

test("title and reply search exclude a moderated reply", async () => {
  const titleMarker = "forum-title-search-marker";
  const replyMarker = "forum-reply-search-marker";
  const threadResponse = await post("/api/threads", { title: titleMarker, body: "A public regression thread.", author: "test-human", model: "bun-test", channel: "commons" });
  expect(threadResponse.status).toBe(201);
  const threadJson = await threadResponse.json() as { data: { id: number } };

  const replyResponse = await post(`/api/threads/${threadJson.data.id}/replies`, { body: replyMarker, author: "test-agent", model: "bun-test" });
  expect(replyResponse.status).toBe(201);
  const replyJson = await replyResponse.json() as { data: { id: number } };
  for (let index = 0; index < 50; index += 1) {
    const paginationReply = await post(`/api/threads/${threadJson.data.id}/replies`, { body: `pagination-reply-${index}`, author: "pagination-agent", model: "bun-test" }, `192.0.2.${index + 1}`);
    expect(paginationReply.status).toBe(201);
  }
  const threadPage = await fetch(`${baseUrl}/t/${threadJson.data.id}`);
  expect(threadPage.status).toBe(200);
  const threadHtml = await threadPage.text();
  expect(threadHtml).toContain("More replies");
  const cursor = threadHtml.match(/reply_cursor=([^#"]+)/u)?.[1];
  expect(cursor).toBeDefined();
  const nextReplyPage = await fetch(`${baseUrl}/t/${threadJson.data.id}?reply_cursor=${cursor}`);
  expect(nextReplyPage.status).toBe(200);
  const nextHtml = await nextReplyPage.text();
  expect(nextHtml).toContain("pagination-reply-49");
  expect(nextHtml).not.toContain("pagination-reply-0");
  const titleSearch = await fetch(`${baseUrl}/api/threads?q=${encodeURIComponent(titleMarker)}`);
  expect(titleSearch.status).toBe(200);
  expect((await titleSearch.json()).data).toHaveLength(1);

  const replySearch = await fetch(`${baseUrl}/api/threads?q=${encodeURIComponent(replyMarker)}`);
  expect(replySearch.status).toBe(200);
  expect((await replySearch.json()).data).toHaveLength(1);

  const moderation = Bun.spawn(["bun", "run", "src/index.ts", "moderate", "post", String(replyJson.data.id)], {
    cwd: repoRoot,
    env: { ...process.env, DATABASE_PATH: databasePath, BASE_PATH: "/free-open-forum-for-agents-to-collaborate", PUBLIC_URL: baseUrl },
    stdout: "ignore",
    stderr: "inherit",
  });
  expect(await moderation.exited).toBe(0);

  const moderatedSearch = await fetch(`${baseUrl}/api/threads?q=${encodeURIComponent(replyMarker)}`);
  expect(moderatedSearch.status).toBe(200);
  expect((await moderatedSearch.json()).data).toHaveLength(0);
});

test("thread structured data safely encodes closing script text", async () => {
  const maliciousBody = `</script><script>alert("not executed")</script>`;
  const response = await post("/api/threads", { title: "Structured data safety", body: maliciousBody, author: "test-agent", model: "bun-test", channel: "commons" }, "198.51.100.1");
  expect(response.status).toBe(201);
  const json = await response.json() as { data: { id: number } };
  const page = await fetch(`${baseUrl}/t/${json.data.id}`);
  expect(page.status).toBe(200);
  const html = await page.text();
  expect(html).not.toContain("</script><script>");
  const match = html.match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/u);
  expect(match).toBeDefined();
  const structured = JSON.parse(match?.[1] || "") as { text?: string };
  expect(structured.text).toBe(maliciousBody);
});
