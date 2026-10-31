# mcp-clientes

«Conectá con Claude»: el MCP de soporte para clientes de Wansoft. Un cliente conecta su Claude con un solo enlace y desde ahí consulta su base de Odoo en solo lectura, consulta su relación con Wansoft y abre una consulta cuando Claude no resuelve.

El diseño completo, con las decisiones y el paso a paso, está en `wansoft_addons/docs/mcp-soporte-clientes.md`. La API de Wansoft que usa este servidor es el módulo `wansoft_claude_connect` de `wansoft_addons`.

| Carpeta | Qué es | Paso |
|---|---|---|
| `servidor/` | El servidor MCP del cliente, uno por persona (imagen `ghcr.io/wansoft-tech/mcp-cliente`) | 03 (tarea 11393) |
| `intermedio/` | Levanta y elimina los servidores en Easypanel (imagen `ghcr.io/wansoft-tech/mcp-provisioner`) | 06 (tarea 11396) |

## Servidor

Se apoya en [`mcp-server-odoo`](https://pypi.org/project/mcp-server-odoo/) 0.9.0 (licencia MPL 2.0) como dependencia, sin modificar sus archivos. Encima agrega:

- **Perfil y nivel.** `MCP_PERFIL=cliente` (por defecto) con `MCP_NIVEL` `lectura` (por defecto), `escritura` (crear y modificar) o `metodos` (además, ejecutar acciones); borrar, nunca. `MCP_PERFIL=equipo`: todo, borrar incluido, sin herramientas de Wansoft. Lo que el nivel no permite no existe en el servidor.
- **Clave del enlace.** Solo atiende `/<clave>/mcp`. Compara la huella SHA-256 de la clave con `MCP_LINK_SECRET_SHA256` en tiempo constante. Cualquier otra ruta o clave responde 404. `/health` queda abierto y responde `{"status": "ok"}`, sin más datos.
- **El nivel manda, no el entorno.** `ODOO_YOLO` y `ODOO_MCP_ENABLE_METHOD_CALLS` se fijan según el nivel aunque vengan otros valores, y solo quedan las herramientas de la lista de permitidas del nivel (`app.py`).
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

## Intermedio

Un solo servicio en Easypanel, en el proyecto `clientes-mcp`, con dominio público `provisioner.mcp.wansoft.tech`. Lo llama el Odoo de Wansoft y **solo responde a pedidos firmados**. Es el único que tiene el token de Easypanel.

| Ruta | Hace |
|---|---|
| `POST /connections` | Crea o actualiza el servidor de una persona del cliente (`c-<id>`), con su nivel (`lectura` por defecto, `escritura` o `metodos`). Espera hasta 20 segundos a que responda |
| `POST /connections/{id}/nivel` | Cambia el nivel sin pedir de nuevo la clave de API: lee las variables del servicio, reemplaza `MCP_NIVEL` y redespliega |
| `GET /connections/{id}` | Si el servicio existe y responde |
| `DELETE /connections/{id}` | Elimina el servicio, con su dominio. Repetirlo no falla |
| `POST /equipo`, `GET` y `DELETE /equipo/{id}` | Lo mismo para el servidor del equipo de Wansoft sobre una base del cliente (`ce-<id>`): perfil `equipo`, acceso completo |
| `GET /health` | `{"status": "ok"}`, sin firma |

**Firma.** Cabeceras `X-Wansoft-Origen` (la dirección del Odoo que llama), `X-Wansoft-Fecha` (segundos Unix) y `X-Wansoft-Firma`: HMAC-SHA256 en hexadecimal de `<fecha>.<cuerpo>`, con el secreto de ese origen. Se rechazan las de más de 5 minutos.

**Un secreto y un prefijo por origen.** Producción crea `c-<id>` y staging `s-<id>`, cada uno con la API de Wansoft de su origen. Staging es una copia de producción: si compartieran secreto, una prueba en staging podría pisar el servidor de un cliente real.

### Variables de entorno

| Variable | Valor |
|---|---|
| `EASYPANEL_URL` | Dirección del panel vista desde el contenedor (`http://easypanel:3000` o la de Tailscale) |
| `EASYPANEL_TOKEN` | La clave de API del usuario de Easypanel. **Solo existe acá** |
| `MCP_IMAGE` | `ghcr.io/wansoft-tech/mcp-cliente:<versión>` |
| `MCP_DOMAIN` | `mcp.wansoft.tech` |
| `MCP_PROJECT` | `clientes-mcp` (por defecto) |
| `ORIGENES` | Una línea por Odoo: `<url> <prefijo> <secreto>`, por ejemplo `https://gnbsas.odoo.com c <secreto>`. Secretos de 32 caracteres o más |

Nunca registra cuerpos ni variables: llevan la clave de API del cliente.
