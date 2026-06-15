export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const match = url.pathname.match(/^\/pool\/([^/]+)\/([^/]+)\/([^/]+)$/);
    const notFound = () => new Response("package redirect not found", {
      status: 404,
      headers: {
        "Cache-Control": "public, max-age=60, s-maxage=60",
        "Cloudflare-CDN-Cache-Control": "public, max-age=60",
        "X-Apt-Index-Redirect-Cache": "MISS",
      },
    });
    if (!match) {
      return notFound();
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", {
        status: 405,
        headers: { "Allow": "GET, HEAD" },
      });
    }

    const cacheUrl = new URL(url);
    cacheUrl.search = "";
    const cacheKey = new Request(cacheUrl.toString(), { method: "GET" });
    const cache = caches.default;
    const cacheGetResponse = (response) => {
      if (request.method === "GET") {
        ctx.waitUntil(cache.put(cacheKey, response.clone()).catch((error) => {
          console.warn("redirect cache put failed", error);
        }));
      }
      return response;
    };
    let cached = await cache.match(cacheKey);
    if (cached) {
      cached = new Response(cached.body, cached);
      cached.headers.set("X-Apt-Index-Redirect-Cache", "HIT");
      return cached;
    }

    const [, component, entryName, filename] = match;
    const rulesUrl = new URL(`/redirect-rules/${component}/${entryName}.json`, url);
    let rules;
    try {
      const rulesResponse = await env.ASSETS.fetch(rulesUrl.toString());
      if (!rulesResponse || !rulesResponse.ok) {
        return cacheGetResponse(notFound());
      }
      rules = await rulesResponse.json();
    } catch (error) {
      console.warn("redirect shard fetch failed", error);
      return cacheGetResponse(notFound());
    }
    const target = rules[filename];
    if (!target) {
      return cacheGetResponse(notFound());
    }

    const redirectResponse = new Response(null, {
      status: 302,
      headers: {
        "Location": target,
        "Cache-Control": "public, max-age=300, s-maxage=2592000",
        "Cloudflare-CDN-Cache-Control": "public, max-age=2592000",
        "X-Apt-Index-Redirect-Cache": "MISS",
      },
    });

    if (request.method === "GET") {
      ctx.waitUntil(cache.put(cacheKey, redirectResponse.clone()).catch((error) => {
        console.warn("redirect cache put failed", error);
      }));
    }

    return redirectResponse;
  },
};
