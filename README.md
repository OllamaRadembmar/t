# BlogTube

Mi versión personal de [TristanTube](https://karpathy.ai/tristan-tube/), el «YouTube sin algoritmo» que
Andrej Karpathy construyó con Claude en noviembre de 2026, ampliado con una segunda pestaña de
**artículos** (Substack y blogs) y una sección de **personas**.

- **Vídeos**: feed «Para ti» aleatorio y ponderado sobre el historial completo de cada canal, filtros por
  núcleo temático y duración, «Sorpréndeme», reproductor con «A continuación» y autoplay, modo TV.
- **Artículos**: el mismo feed para blogs, con lector limpio (tipografía Literata, tamaño ajustable, barra de
  progreso) para los posts abiertos y enlace al original para los de pago.
- **Personas**: sus canales y blogs, entrevistas y conferencias encontradas en YouTube y artículos que las mencionan.
- Sin servidor, sin dominio, sin coste: una página estática en GitHub Pages.

## Cómo funciona

```
sources.toml ──► build.py ──► cache/        (todo lo descargado, acumulativo)
                    │
                    └──────► site/data/     (meta.json, videos.json, articles.json, a/<id>.json)
                                  │
                  site/index.html ┘  (una sola página HTML+CSS+JS, sin dependencias)
```

- Canales: `yt-dlp` lista el historial completo (sin Shorts) + RSS oficial de YouTube para fechas exactas.
- Blogs: RSS + archivo público de Substack (todos los posts; el texto de los abiertos se guarda en `site/data/a/`).
- Personas: búsquedas en YouTube; solo se aceptan vídeos cuyo título menciona a la persona.
- Una GitHub Action (`.github/workflows/update.yml`) lo repite cada lunes y publica en GitHub Pages.

## Uso local

```powershell
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe build.py            # incremental
.venv\Scripts\python.exe build.py --full     # re-listar todos los canales enteros
.venv\Scripts\python.exe -m http.server 8765 -d site
```

Abre http://localhost:8765

Opciones útiles de `build.py`: `--solo videos|blogs|personas`, `--only "Ernesto"`, `--offline` (recompone sin descargar).

## Añadir o quitar contenido

Todo está en [`sources.toml`](sources.toml): copia un bloque `[[canal]]`, `[[blog]]` o `[[persona]]`, cambia los datos
y guarda. Para encontrar el `id` de un canal de YouTube, abre el canal → «Compartir canal» → «Copiar ID del canal».

## Publicación (privada: Cloudflare Pages + usuario y contraseña)

El repositorio es **privado**. Cada lunes (o a mano en **Actions → Actualizar y publicar → Run workflow**),
GitHub Actions ejecuta `build.py` y publica `site/` en **Cloudflare Pages**. Antes de servir cualquier archivo,
[`functions/_middleware.js`](functions/_middleware.js) exige **usuario y contraseña** en el servidor de Cloudflare;
la sesión dura 30 días y se cierra desde «Acerca → Cerrar sesión».

Secretos del repositorio (Settings → Secrets and variables → Actions):

| Secreto | Qué es |
|---|---|
| `CLOUDFLARE_API_TOKEN` | Token de Cloudflare con permiso *Account · Cloudflare Pages · Edit* |
| `CLOUDFLARE_ACCOUNT_ID` | ID de la cuenta de Cloudflare |
| `BLOG_USER` / `BLOG_PASS` | Usuario y contraseña para entrar en la web |

Para cambiar la contraseña: `gh secret set BLOG_PASS` y relanzar el workflow (cierra todas las sesiones abiertas).

Probar el login en local: crea `.dev.vars` con `BLOG_USER=...` y `BLOG_PASS=...` y ejecuta
`npx wrangler@4 pages dev site --port 8788`.
