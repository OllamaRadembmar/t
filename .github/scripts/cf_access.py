"""Protege la web de Cloudflare Pages con Cloudflare Access: solo entran los emails autorizados
(reciben un código de un solo uso). Idempotente: se puede ejecutar varias veces.

Variables de entorno: CF_TOKEN, CF_ACCOUNT, ACCESS_EMAILS (separados por comas), PROJECT (opcional).
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"
TOKEN, ACC = os.environ["CF_TOKEN"], os.environ["CF_ACCOUNT"]
EMAILS = [e.strip() for e in os.environ.get("ACCESS_EMAILS", "").split(",") if e.strip()]
PROJECT = os.environ.get("PROJECT") or "blogtube"
APP_NAME, POLICY_NAME = "BlogTube", "BlogTube · solo yo"


def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        js = json.loads(e.read() or b"{}")
        errs = "; ".join(f"{x.get('code')}: {x.get('message')}" for x in js.get("errors", []))
        sys.exit(f"✗ {method} {path} → HTTP {e.code}: {errs}")


if not EMAILS:
    sys.exit("✗ Falta el secreto ACCESS_EMAILS (tu email, o varios separados por comas).")

org = call("GET", f"/accounts/{ACC}/access/organizations")
if not org.get("result"):
    sys.exit("✗ Zero Trust no está activado. Entra en one.dash.cloudflare.com, elige un nombre de equipo y el plan Free.")
print("✓ Zero Trust activo:", org["result"].get("auth_domain"))

proj = call("GET", f"/accounts/{ACC}/pages/projects/{PROJECT}")["result"]
host = proj["subdomain"]
print("✓ Proyecto Pages:", host)

include = [{"email": {"email": e}} for e in EMAILS]
policies = call("GET", f"/accounts/{ACC}/access/policies")["result"] or []
pol = next((p for p in policies if p.get("name") == POLICY_NAME), None)
body = {"name": POLICY_NAME, "decision": "allow", "include": include}
pol = call("PUT", f"/accounts/{ACC}/access/policies/{pol['id']}", body)["result"] if pol else \
    call("POST", f"/accounts/{ACC}/access/policies", body)["result"]
print(f"✓ Política «{POLICY_NAME}»: {len(EMAILS)} email(s) autorizado(s)")

apps = call("GET", f"/accounts/{ACC}/access/apps")["result"] or []
app = next((a for a in apps if a.get("name") == APP_NAME), None)
body = {
    "name": APP_NAME, "type": "self_hosted", "domain": host,
    "destinations": [{"type": "public", "uri": host}, {"type": "public", "uri": f"*.{host}"}],
    "session_duration": "720h", "app_launcher_visible": False,
    "policies": [{"id": pol["id"], "precedence": 1}],
}
app = call("PUT", f"/accounts/{ACC}/access/apps/{app['id']}", body)["result"] if app else \
    call("POST", f"/accounts/{ACC}/access/apps", body)["result"]
print(f"✓ Access protege https://{host} (y sus vistas previas). Sesión: 30 días.")
