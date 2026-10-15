import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import { ApiError, AutoNXT, SignatureVerificationError, constructEvent, verifySignature } from "../src/index.js";

function fakeFetch(responses) {
  const seen = [];
  const impl = async (url, init) => {
    seen.push({ url: String(url), init });
    const { status = 200, body = {} } = responses.shift() ?? {};
    return { ok: status < 400, status, text: async () => JSON.stringify(body) };
  };
  return { impl, seen };
}

test("sends auth, query params, idempotency key and parses errors", async () => {
  const { impl, seen } = fakeFetch([{ body: [{ id: "1" }] }, { status: 201, body: { id: "c" } }, { status: 404, body: { detail: "Customer not found" } }]);
  const client = new AutoNXT("ak_test_x", { baseUrl: "https://api.test/api/v1/", fetch: impl });
  assert.deepEqual(await client.customers.list({ limit: 5 }), [{ id: "1" }]);
  assert.equal(seen[0].url, "https://api.test/api/v1/customers?limit=5&offset=0");
  assert.equal(seen[0].init.headers.Authorization, "Bearer ak_test_x");

  await client.calls.create({ direction: "outbound", to_number: "+1", from_number: "+2", ai_agent_id: "a" });
  assert.ok(seen[1].init.headers["Idempotency-Key"]);
  assert.equal(JSON.parse(seen[1].init.body).direction, "outbound");

  await assert.rejects(client.customers.get("x"), (e) => e instanceof ApiError && e.status === 404 && e.detail === "Customer not found");
});

test("webhook signature verification", () => {
  const body = '{"event":"call.ended"}';
  const ts = Math.floor(Date.now() / 1000);
  const sig = createHmac("sha256", "whsec_x").update(`${ts}.${body}`).digest("hex");
  assert.equal(verifySignature("whsec_x", ts, body, sig), true);
  assert.equal(verifySignature("whsec_x", ts, body + " ", sig), false);
  assert.equal(verifySignature("whsec_x", ts - 1000, body, createHmac("sha256", "whsec_x").update(`${ts - 1000}.${body}`).digest("hex")), false);
  assert.equal(constructEvent("whsec_x", { "X-Timestamp": String(ts), "X-Signature": sig }, body).event, "call.ended");
  assert.throws(() => constructEvent("whsec_x", { "X-Timestamp": String(ts), "X-Signature": "bad" }, body), SignatureVerificationError);
});
