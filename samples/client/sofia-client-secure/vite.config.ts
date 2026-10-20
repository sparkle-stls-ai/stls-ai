import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import http from 'node:http'
import https from 'node:https'
import { URL } from 'node:url'
import type { IncomingMessage, ServerResponse } from 'node:http'
import { ProxyAgent } from 'undici'
import pkg from './package.json'

/**
 * Returns true if targetHostname should bypass the proxy.
 * Follows standard no_proxy rules:
 *   - "*"                → bypass everything
 *   - "localhost"        → exact hostname match
 *   - "127.0.0.1"        → exact IP match
 *   - ".example.com"     → any subdomain (and the domain itself)
 *   - "example.com"      → exact domain match
 * Port is ignored in comparisons (e.g. "localhost:8080" → matches any port on localhost).
 */
function matchesNoProxy(targetHostname: string, noProxyList: string): boolean {
  if (!noProxyList.trim()) return false
  const host = targetHostname.toLowerCase()
  for (const raw of noProxyList.split(',')) {
    const entry = raw.trim().toLowerCase()
    if (!entry) continue
    if (entry === '*') return true
    // Strip optional port from the no_proxy entry
    const entryHost = entry.split(':')[0]
    if (entryHost.startsWith('.')) {
      // .example.com → matches sub.example.com AND example.com
      const domain = entryHost.slice(1)
      if (host === domain || host.endsWith('.' + domain)) return true
    } else {
      if (host === entryHost) return true
    }
  }
  return false
}

/**
 * A2A dynamic proxy plugin.
 * Routes /a2a-proxy requests to the real agent URL (passed via x-a2a-target header).
 * Adds CORS headers on the way back so the browser never complains.
 *
 * Optional: if the client sets x-a2a-proxy-url, all outbound requests are routed
 * through that HTTP/HTTPS proxy using undici's ProxyAgent (supports CONNECT for HTTPS).
 *
 * Only active during `vite dev` — production builds call agents directly.
 */
function a2aProxyPlugin() {
  return {
    name: 'a2a-proxy',
    configureServer(server: {
      middlewares: {
        use: (
          path: string,
          handler: (req: IncomingMessage, res: ServerResponse, next: () => void) => void,
        ) => void
      }
    }) {
      server.middlewares.use('/a2a-proxy', (req, res, next) => {
        const targetUrl = decodeURIComponent((req.headers['x-a2a-target'] as string) ?? '')
        if (!targetUrl) { next(); return }

        const rawProxy = req.headers['x-a2a-proxy-url']
        const proxyUrl = Array.isArray(rawProxy) ? rawProxy[0] : (rawProxy as string | undefined)

        const rawNoProxy = req.headers['x-a2a-no-proxy']
        const noProxyList = Array.isArray(rawNoProxy) ? rawNoProxy[0] : (rawNoProxy as string | undefined) ?? ''

        // Collect request body
        const chunks: Buffer[] = []
        req.on('data', (c: Buffer) => chunks.push(c))
        req.on('end', () => {
          const body = Buffer.concat(chunks)

          // Build forwarded headers (strip internal ones)
          const fwdHeaders: Record<string, string | string[] | undefined> = {}
          for (const [k, v] of Object.entries(req.headers)) {
            if (k !== 'x-a2a-target' && k !== 'x-a2a-proxy-url' && k !== 'x-a2a-no-proxy' && k !== 'host') {
              fwdHeaders[k] = v
            }
          }

          // ── Proxy path: route through the user-supplied HTTP proxy ──────────
          // Skip proxy if the target hostname is in the no_proxy list
          const parsed = new URL(targetUrl)
          const useProxy = !!proxyUrl && !matchesNoProxy(parsed.hostname, noProxyList)

          if (useProxy) {
            const dispatcher = new ProxyAgent(proxyUrl)

            // Flatten headers for undici (no undefined values, no arrays)
            const flatHeaders: Record<string, string> = {}
            for (const [k, v] of Object.entries(fwdHeaders)) {
              if (v === undefined) continue
              flatHeaders[k] = Array.isArray(v) ? v.join(', ') : v
            }

            ;(async () => {
              try {
                const { fetch: undiciFetch } = await import('undici')
                const upstreamRes = await undiciFetch(targetUrl, {
                  method: req.method ?? 'GET',
                  headers: flatHeaders,
                  body: body.length ? body : undefined,
                  dispatcher,
                  // @ts-ignore — undici-specific; prevents buffering large SSE streams
                  duplex: 'half',
                })

                const resHeaders: Record<string, string | string[]> = {
                  'access-control-allow-origin': '*',
                  'access-control-allow-headers': '*',
                  'access-control-allow-methods': '*',
                }
                upstreamRes.headers.forEach((v: string, k: string) => {
                  resHeaders[k] = v
                })
                res.writeHead(upstreamRes.status, resHeaders)

                if (upstreamRes.body) {
                  const reader = upstreamRes.body.getReader()
                  while (true) {
                    const { done, value } = await reader.read()
                    if (done) break
                    res.write(value)
                  }
                }
                res.end()
              } catch (err) {
                if (!res.headersSent) res.writeHead(502)
                res.end(JSON.stringify({ error: err instanceof Error ? err.message : String(err) }))
              }
            })()
            return
          }

          // ── Direct path: use Node http/https (no proxy) ──────────────────────
          const lib = parsed.protocol === 'https:' ? https : http

          const parsedHost = parsed.host
          if (body.length) fwdHeaders['content-length'] = String(body.length)
          fwdHeaders['host'] = parsedHost

          const options = {
            hostname: parsed.hostname,
            port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
            path: parsed.pathname + parsed.search,
            method: req.method,
            headers: fwdHeaders,
          }

          const proxyReq = lib.request(options, (proxyRes) => {
            const resHeaders: Record<string, string | string[]> = {
              'access-control-allow-origin': '*',
              'access-control-allow-headers': '*',
              'access-control-allow-methods': '*',
            }
            for (const [k, v] of Object.entries(proxyRes.headers)) {
              if (v !== undefined) resHeaders[k] = v
            }
            res.writeHead(proxyRes.statusCode ?? 200, resHeaders)
            proxyRes.pipe(res, { end: true })
          })

          proxyReq.on('error', (err: Error) => {
            if (!res.headersSent) res.writeHead(502)
            res.end(JSON.stringify({ error: err.message }))
          })

          if (body.length) proxyReq.write(body)
          proxyReq.end()
        })
      })
    },
  }
}

export default defineConfig({
  // VITE_BASE is a build-time arg (e.g. '/a/') set via --build-arg in Docker.
  // Must match BASE_PATH runtime env on the Express server.
  // Leave unset (defaults to '/') when served at the domain root.
  base: process.env.VITE_BASE ?? '/',
  // App version: single source of truth is package.json (shown in Settings)
  define: {
    __APP_VERSION__: JSON.stringify(pkg.version),
  },
  plugins: [react(), a2aProxyPlugin()],
  server: {
    port: 5173,
    proxy: {
      // In dev, forward /api/* to the Express backend
      '/api': {
        target: 'http://localhost:3000',
        changeOrigin: true,
        secure: false,
      },
    },
  },
})
