/**
 * Cloudflare Worker: Proxy scraper
 * 
 * Deploy: wrangler deploy
 * Use:   https://your-worker.workers.dev/?url=https://www.datasheets.com/...
 * 
 * Note: Set CLOUDSCRAPER=1 env var to use cloudscraper inside the worker.
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url).searchParams.get("url");
    if (!url) {
      return new Response("Missing ?url= parameter", { status: 400 });
    }

    try {
      const targetUrl = new URL(url);
      if (targetUrl.hostname !== "www.datasheets.com") {
        return new Response("Only datasheets.com allowed", { status: 403 });
      }

      // Use cloudscraper if env var is set
      if (env.CLOUDSCRAPER === "1") {
        const { default: cloudscraper } = await import("cloudscraper");
        const response = await cloudscraper({
          uri: url,
          cloudflareTimeout: 8000,
          cache: true,
          retry: 2,
          maxRetries: 2,
        });
        return new Response(response, {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "X-Proxy-Status": "cloudscraper",
          },
        });
      }

      // Regular fetch
      const response = await fetch(url, {
        headers: {
          "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9",
        },
      });

      const body = await response.text();
      return new Response(body, {
        status: response.status,
        headers: {
          "Content-Type": "text/html; charset=utf-8",
          "X-Proxy-Status": String(response.status),
          "X-Original-URL": url,
        },
      });
    } catch (err) {
      return new Response(`Proxy error: ${err.message}`, { status: 502 });
    }
  },
};
