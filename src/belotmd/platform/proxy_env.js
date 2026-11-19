/**
 * Optional outbound proxy for belot.md HTTP + Colyseus WebSocket.
 *
 * Env (first match wins):
 *   BELOT_PROXY / HTTPS_PROXY / HTTP_PROXY
 * Examples:
 *   http://127.0.0.1:7890
 *   socks5://127.0.0.1:1080
 *
 * Must be imported BEFORE colyseus.js so globalThis.WebSocket is patched
 * before Colyseus binds its transport.
 */
import WebSocket from "ws";
import { ProxyAgent, fetch as undiciFetch } from "undici";
import { HttpsProxyAgent } from "https-proxy-agent";
import { SocksProxyAgent } from "socks-proxy-agent";
import http from "node:http";
import https from "node:https";

const raw = (
    process.env.BELOT_PROXY
    || process.env.HTTPS_PROXY
    || process.env.HTTP_PROXY
    || ""
).trim();

export const PROXY_URL = raw;
const isSocks = /^socks/i.test(PROXY_URL);

/** Agent for `ws` / Colyseus (HTTP CONNECT or SOCKS). */
export const wsProxyAgent = PROXY_URL
    ? (isSocks ? new SocksProxyAgent(PROXY_URL) : new HttpsProxyAgent(PROXY_URL))
    : null;

/** undici dispatcher for HTTP(S) proxies only. */
const httpDispatcher = (PROXY_URL && !isSocks)
    ? new ProxyAgent(PROXY_URL)
    : null;

if (wsProxyAgent) {
    const Base = WebSocket;
    globalThis.WebSocket = class ProxiedWebSocket extends Base {
        constructor(url, protocols, options = {}) {
            super(url, protocols, { ...options, agent: wsProxyAgent });
        }
    };
    const redacted = PROXY_URL.replace(/\/\/([^/@]+@)/, "//***@");
    console.log(`[Bridge] outbound proxy: ${redacted}`);
}

/**
 * fetch() that honors BELOT_PROXY. SOCKS uses a small http(s) helper;
 * HTTP proxies go through undici ProxyAgent.
 */
export async function bridgeFetch(url, opts = {}) {
    if (!PROXY_URL) {
        return fetch(url, opts);
    }
    if (httpDispatcher) {
        return undiciFetch(url, { ...opts, dispatcher: httpDispatcher });
    }
    // SOCKS: undici has no built-in socks dispatcher → node http(s) + agent.
    return socksFetch(String(url), opts);
}

function socksFetch(url, opts = {}) {
    const u = new URL(url);
    const lib = u.protocol === "http:" ? http : https;
    const method = (opts.method || "GET").toUpperCase();
    const headers = { ...(opts.headers || {}) };
    let body = opts.body;
    if (body && typeof body !== "string" && !Buffer.isBuffer(body)) {
        body = String(body);
    }
    const signal = opts.signal;

    return new Promise((resolve, reject) => {
        const onAbort = () => {
            req.destroy();
            reject(signal?.reason || new Error("aborted"));
        };
        if (signal) {
            if (signal.aborted) {
                reject(signal.reason || new Error("aborted"));
                return;
            }
            signal.addEventListener("abort", onAbort, { once: true });
        }
        const req = lib.request({
            protocol: u.protocol,
            hostname: u.hostname,
            port: u.port || (u.protocol === "http:" ? 80 : 443),
            path: u.pathname + u.search,
            method,
            headers,
            agent: wsProxyAgent,
        }, (res) => {
            const chunks = [];
            res.on("data", (c) => chunks.push(c));
            res.on("end", () => {
                if (signal) signal.removeEventListener("abort", onAbort);
                const buf = Buffer.concat(chunks);
                resolve({
                    ok: res.statusCode >= 200 && res.statusCode < 300,
                    status: res.statusCode,
                    async text() { return buf.toString("utf8"); },
                    async json() { return JSON.parse(buf.toString("utf8")); },
                });
            });
        });
        req.on("error", (err) => {
            if (signal) signal.removeEventListener("abort", onAbort);
            reject(err);
        });
        if (body) req.write(body);
        req.end();
    });
}
