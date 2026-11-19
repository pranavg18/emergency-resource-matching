"use strict";

// The page's one way to call the API. Every call goes to this page's own
// origin (/api/...); client/web.py forwards it to the API or coordinator.
//
// The proxy accepts writes only as JSON, so every POST and PUT carries a JSON
// body, {} when the endpoint takes no input (such as POST /match/{id}). Doing
// this here rather than at each call site means no button can forget it.
// Kept separate from app.js so tests can load it without the map or a DOM.

const WRITE_METHODS = new Set(["POST", "PUT"]);

async function api(method, path, body) {
  const init = { method, headers: {} };
  if (WRITE_METHODS.has(method)) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  let response;
  try {
    response = await fetch(`/api/${path}`, init);
  } catch (err) {
    return { ok: false, status: 0, data: { error: { code: "NETWORK", message: String(err) } } };
  }
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  return { ok: response.ok, status: response.status, data };
}
