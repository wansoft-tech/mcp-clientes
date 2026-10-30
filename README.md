# mcp-clientes

«Conectá con Claude»: el MCP de soporte para clientes de Wansoft. Un cliente conecta su Claude con un solo enlace y desde ahí consulta su base de Odoo en solo lectura, consulta su relación con Wansoft y abre una consulta cuando Claude no resuelve.

El diseño completo, con las decisiones y el paso a paso, está en `wansoft_addons/docs/mcp-soporte-clientes.md`. La API de Wansoft que usa este servidor es el módulo `wansoft_claude_connect` de `wansoft_addons`.

| Carpeta | Qué es | Paso |
|---|---|---|
| `servidor/` | El servidor MCP del cliente, uno por persona (imagen `ghcr.io/wansoft-tech/mcp-cliente`) | 03 (tarea 11393) |
| `intermedio/` | Levanta y elimina los servidores en Easypanel | 06 (tarea 11396), todavía no existe |

## Servidor

Se apoya en [`mcp-server-odoo`](https://pypi.org/project/mcp-server-odoo/) 0.9.0 (licencia MPL 2.0) como dependencia, sin modificar sus archivos. Encima agrega:

- **Clave del enlace.** Solo atiende `/<clave>/mcp`. Compara la huella SHA-256 de la clave con `MCP_LINK_SECRET_SHA256` en tiempo constante. Cualquier otra ruta o clave responde 404. `/health` queda abierto y responde `{"status": "ok"}`, sin más datos.
- **Solo lectura forzada.** `ODOO_YOLO` se fuerza a `read` y se descarta `ODOO_MCP_ENABLE_METHOD_CALLS` aunque vengan en el entorno. Solo quedan las herramientas de una lista de permitidas (`app.py`, `LECTURA`); las de escritura no existen en el servidor.
- **Herramientas de Wansoft**, con prefijo `wansoft_`. No dependen de que la base del cliente responda: si su Odoo está caído, igual puede abrir una consulta.
- **Instrucciones para Claude**, en español (`instrucciones.py`).
- **Sin logs de acceso:** la ruta lleva la clave del enlace.

### Variables de entorno

Las carga el intermedio, una vez por persona.

| Variable | Valor |
|---|---|
| `ODOO_URL`, `ODOO_DB` | Su Odoo |
| `ODOO_USER`, `ODOO_API_KEY` | Su usuario y su clave de API |
| `ODOO_ALLOWED_COMPANIES` | Opcional |
| `ODOO_MCP_ALLOWED_HOSTS` | `c-<id>.mcp.wansoft.tech` |
| `MCP_LINK_SECRET_SHA256` | Huella de la clave del enlace |
| `WANSOFT_API_URL` | `https://<odoo de wansoft>/wansoft/mcp/v1` |
| `WANSOFT_TOKEN` | Token de la conexión |

`ODOO_MCP_HOST` y `ODOO_MCP_PORT` vienen en la imagen: `0.0.0.0` y `8000`.

### Desarrollo

```bash
cd servidor
pip install -e '.[dev]'
pytest -q
```

Los tests no necesitan Odoo: el original conecta recién en la primera llamada a su base, y Wansoft se simula con un servidor local.

### Publicar una imagen

Una etiqueta `servidor-v<versión>` corre los tests y, si pasan, publica `ghcr.io/wansoft-tech/mcp-cliente:<versión>`. También se puede publicar a mano desde Actions («Run workflow»): toma la versión de `servidor/pyproject.toml`.

```bash
git tag servidor-v0.1.0 && git push origin servidor-v0.1.0
```
