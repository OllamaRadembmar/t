/**
 * BlogTube · acceso con usuario y contraseña (Cloudflare Pages Functions).
 *
 * Se ejecuta en el servidor de Cloudflare ANTES de servir cualquier archivo:
 * sin sesión válida no se entrega nada (ni la página ni los datos).
 * Usuario y contraseña viven como secretos del proyecto de Pages: BLOG_USER y BLOG_PASS.
 * La sesión es una cookie firmada (HMAC-SHA256) que dura 30 días; cambiar la
 * contraseña invalida todas las sesiones.
 */
const COOKIE = "bt_session";
const DAYS = 30;
const enc = new TextEncoder();

async function hmac(key, msg) {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(msg)));
  return btoa(String.fromCharCode(...sig)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function same(a, b) {               // comparación en tiempo constante
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
const secret = env => `blogtube|${env.BLOG_USER}|${env.BLOG_PASS}`;
async function newToken(env) {
  const exp = String(Date.now() + DAYS * 864e5);
  return `${exp}.${await hmac(secret(env), exp)}`;
}
async function validToken(env, tok) {
  if (!tok) return false;
  const [exp, sig] = tok.split(".");
  if (!exp || !sig || Number(exp) < Date.now()) return false;
  return same(sig, await hmac(secret(env), exp));
}
function cookie(req, name) {
  const m = (req.headers.get("Cookie") || "").match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return m ? decodeURIComponent(m[1]) : null;
}
const safeNext = n => (typeof n === "string" && n.startsWith("/") && !n.startsWith("//") && !n.startsWith("/login")) ? n : "/";
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function loginPage(next, failed) {
  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>BlogTube · Entrar</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 30 22'%3E%3Crect width='30' height='22' rx='6' fill='%23e5484d'/%3E%3Cpath d='M12 6.5v9l8-4.5z' fill='%23fff'/%3E%3C/svg%3E">
<style>
:root{--bg:#0f0f0f;--bg2:#181818;--bg3:#272727;--bg4:#3a3a3a;--fg:#f1f1f1;--fg2:#aaa;--accent:#e5484d;color-scheme:dark}
@media (prefers-color-scheme: light){:root{--bg:#fff;--bg2:#f6f6f6;--bg3:#ececec;--bg4:#d9d9d9;--fg:#0f0f0f;--fg2:#606060;color-scheme:light}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:15px/1.4 Roboto,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
main{min-height:100%;display:grid;place-items:center;padding:24px 16px}
form{width:100%;max-width:360px;background:var(--bg2);border:1px solid var(--bg3);border-radius:16px;padding:28px 24px}
.logo{display:flex;align-items:center;gap:8px;font-weight:700;font-size:22px;letter-spacing:-.5px;margin-bottom:6px}
p{color:var(--fg2);margin:0 0 20px}label{display:block;font-size:13px;font-weight:500;margin:14px 0 6px}
input{width:100%;height:44px;padding:0 14px;border-radius:10px;border:1px solid var(--bg4);background:var(--bg);color:var(--fg);font-size:16px;outline:none}
input:focus{border-color:#1c62b9}
button{width:100%;height:44px;margin-top:22px;border:0;border-radius:22px;background:var(--fg);color:var(--bg);font-weight:600;font-size:15px;cursor:pointer}
.err{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--fg);border-radius:10px;padding:10px 12px;font-size:14px;margin-bottom:6px}
</style></head><body><main>
<form method="post" action="/login">
  <div class="logo"><svg width="30" height="22" viewBox="0 0 30 22"><rect width="30" height="22" rx="6" fill="#e5484d"/><path d="M12 6.5v9l8-4.5z" fill="#fff"/></svg>BlogTube</div>
  <p>Acceso privado.</p>
  ${failed ? `<div class="err">Usuario o contraseña incorrectos.</div>` : ""}
  <input type="hidden" name="next" value="${esc(next)}">
  <label for="u">Usuario</label><input id="u" name="user" autocomplete="username" autocapitalize="none" required autofocus>
  <label for="p">Contraseña</label><input id="p" name="pass" type="password" autocomplete="current-password" required>
  <button type="submit">Entrar</button>
</form></main></body></html>`;
  return new Response(html, {
    status: failed ? 401 : 200,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow" },
  });
}

export async function onRequest({ request, env, next }) {
  const url = new URL(request.url);
  if (!env.BLOG_USER || !env.BLOG_PASS) {
    return new Response("BlogTube: faltan los secretos BLOG_USER / BLOG_PASS en Cloudflare Pages.", { status: 503 });
  }

  if (url.pathname === "/login") {
    if (request.method === "POST") {
      const form = await request.formData();
      const user = String(form.get("user") || ""), pass = String(form.get("pass") || "");
      const ok = same(await hmac("cmp", `${user}\n${pass}`), await hmac("cmp", `${env.BLOG_USER}\n${env.BLOG_PASS}`));
      if (ok) {
        return new Response(null, { status: 303, headers: {
          Location: safeNext(form.get("next")),
          "Set-Cookie": `${COOKIE}=${await newToken(env)}; Path=/; Max-Age=${DAYS * 86400}; HttpOnly; Secure; SameSite=Lax`,
        } });
      }
      await new Promise(r => setTimeout(r, 1500));     // frena los intentos a ciegas
      return loginPage(safeNext(form.get("next")), true);
    }
    return loginPage(safeNext(url.searchParams.get("next")), false);
  }

  if (url.pathname === "/logout") {
    return new Response(null, { status: 303, headers: {
      Location: "/login", "Set-Cookie": `${COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax`,
    } });
  }

  if (await validToken(env, cookie(request, COOKIE))) {
    const res = await next();
    const out = new Response(res.body, res);
    out.headers.set("Cache-Control", "private, no-cache");
    out.headers.set("X-Robots-Tag", "noindex, nofollow");
    return out;
  }

  const wantsPage = (request.headers.get("Accept") || "").includes("text/html");
  if (!wantsPage) return new Response("No autorizado", { status: 401, headers: { "Cache-Control": "no-store" } });
  return Response.redirect(`${url.origin}/login?next=${encodeURIComponent(url.pathname + url.search)}`, 302);
}
