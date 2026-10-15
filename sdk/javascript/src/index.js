import { randomUUID } from "node:crypto";
export { computeSignature, constructEvent, SignatureVerificationError, verifySignature } from "./webhooks.js";

export class ApiError extends Error {
  constructor(status, detail) {
    super(`HTTP ${status}: ${typeof detail === "string" ? detail : JSON.stringify(detail)}`);
    this.status = status;
    this.detail = detail;
  }
}

/**
 * Client for the AutoNXT public API.
 *   const client = new AutoNXT("ak_live_...", { baseUrl: "https://api.example.com/api/v1" });
 *   await client.customers.create({ name: "Jane", phone: "+919800000000" });
 */
export class AutoNXT {
  constructor(apiKey, { baseUrl = "http://localhost:8000/api/v1", fetch: fetchImpl = globalThis.fetch } = {}) {
    if (!apiKey) throw new Error("apiKey is required");
    this.apiKey = apiKey;
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this._fetch = fetchImpl;
    const r = (m, p, o) => this.request(m, p, o);

    this.calls = {
      list: (filters) => r("GET", "/calls", { params: filters }),
      get: (id) => r("GET", `/calls/${id}`),
      // Retries with the same idempotencyKey never dial twice (generated if omitted).
      create: (body, { idempotencyKey = randomUUID() } = {}) =>
        r("POST", "/calls", { body, headers: { "Idempotency-Key": idempotencyKey } }),
      transcript: (id) => r("GET", `/calls/${id}/transcript`),
    };
    this.customers = {
      list: ({ limit = 50, offset = 0 } = {}) => r("GET", "/customers", { params: { limit, offset } }),
      get: (id) => r("GET", `/customers/${id}`),
      create: (body) => r("POST", "/customers", { body }),
      update: (id, body) => r("PATCH", `/customers/${id}`, { body }),
      delete: (id) => r("DELETE", `/customers/${id}`),
    };
    this.campaigns = {
      list: () => r("GET", "/campaigns"),
      get: (id) => r("GET", `/campaigns/${id}`),
      create: (body) => r("POST", "/campaigns", { body }),
      addContacts: (id, customerIds) => r("POST", `/campaigns/${id}/contacts`, { body: { customer_ids: customerIds } }),
      contacts: (id) => r("GET", `/campaigns/${id}/contacts`),
      launch: (id) => r("POST", `/campaigns/${id}/launch`),
      pause: (id) => r("POST", `/campaigns/${id}/pause`),
      resume: (id) => r("POST", `/campaigns/${id}/resume`),
    };
    this.analytics = {
      overview: () => r("GET", "/analytics/overview"),
      calls: (params) => r("GET", "/analytics/calls", { params }),
      tickets: (params) => r("GET", "/analytics/tickets", { params }),
      campaigns: (params) => r("GET", "/analytics/campaigns", { params }),
    };
    this.aiAgents = {
      list: () => r("GET", "/ai-agents"),
      get: (id) => r("GET", `/ai-agents/${id}`),
      create: (body) => r("POST", "/ai-agents", { body }),
      update: (id, body) => r("PATCH", `/ai-agents/${id}`, { body }),
    };
    this.webhooks = {
      list: () => r("GET", "/developer/webhooks"),
      // The signing secret is returned once, in this response.
      create: (body) => r("POST", "/developer/webhooks", { body }),
      update: (id, body) => r("PATCH", `/developer/webhooks/${id}`, { body }),
      delete: (id) => r("DELETE", `/developer/webhooks/${id}`),
      test: (id) => r("POST", `/developer/webhooks/${id}/test`),
      deliveries: (limit = 50) => r("GET", "/developer/webhooks/deliveries", { params: { limit } }),
    };
  }

  async request(method, path, { params, body, headers } = {}) {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(params ?? {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const response = await this._fetch(url, {
      method,
      headers: {
        Authorization: `Bearer ${this.apiKey}`,
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!response.ok) throw new ApiError(response.status, data?.detail ?? data);
    return data;
  }
}
