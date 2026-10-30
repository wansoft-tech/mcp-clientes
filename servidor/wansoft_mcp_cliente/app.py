"""Arma el servidor: mcp-server-odoo en solo lectura, más Wansoft, detrás de la clave del enlace.

Sobre mcp-server-odoo 0.9.0, sin modificar sus archivos:

1. Solo lectura forzada. ODOO_YOLO=read antes de leer la configuración, y las
   herramientas que no están en LECTURA se sacan del servidor: en modo read el
   original las registra igual y las rechaza al llamarlas; acá ni existen.
2. Las herramientas de Wansoft, con prefijo wansoft_.
3. Las instrucciones para Claude, en español.
4. La clave del enlace: solo se atiende /<clave>/mcp. Cualquier otra ruta o
   clave responde 404 sin tocar Odoo. /health queda abierto y no dice nada.
"""
import hashlib
import hmac
import json
import os

from mcp_server_odoo.config import get_config
from mcp_server_odoo.server import OdooMCPServer
from mcp_server_odoo.user_context import usage_guidance

from . import wansoft
from .instrucciones import INSTRUCCIONES

# Lo único que el servidor del cliente puede hacer en su base. Una lista de
# permitidas y no de prohibidas: una herramienta nueva de una versión futura
# del original queda afuera hasta que alguien la sume a propósito.
LECTURA = frozenset({
    'search_records',
    'get_record',
    'aggregate_records',
    'list_models',
    'get_fields',
    'get_current_context',
    'list_resource_templates',
    'read_attachment',
    'list_record_attachments',
})

RUTA_MCP = '/mcp'


def forzar_solo_lectura(entorno=os.environ) -> None:
    entorno['ODOO_YOLO'] = 'read'
    entorno.pop('ODOO_MCP_ENABLE_METHOD_CALLS', None)
    entorno['ODOO_MCP_TRANSPORT'] = 'streamable-http'


def armar_servidor() -> OdooMCPServer:
    forzar_solo_lectura()
    config = get_config()
    servidor = OdooMCPServer(config)
    app = servidor.app

    for herramienta in list(app._tool_manager.list_tools()):
        if herramienta.name not in LECTURA:
            app.remove_tool(herramienta.name)

    api = wansoft.ApiWansoft(os.environ['WANSOFT_API_URL'], os.environ['WANSOFT_TOKEN'])
    wansoft.registrar(app, api)
    separar_wansoft_de_su_base(app)

    # El original personaliza las instrucciones con el usuario conectado a
    # partir de _static_instructions: se reemplaza esa base.
    uso = usage_guidance(h.name for h in app._tool_manager.list_tools())
    servidor._static_instructions = '%s\n\n%s' % (INSTRUCCIONES, uso) if uso else INSTRUCCIONES
    app._lowlevel_server.instructions = servidor._static_instructions
    return servidor


def separar_wansoft_de_su_base(app) -> None:
    """Las herramientas de Wansoft no dependen de que su Odoo responda.

    El original pone delante de toda llamada la conexión a la base del
    cliente. Con eso, una base caída dejaría también sin poder abrir una
    consulta a Wansoft, que es justo cuando más falta hace. Las wansoft_ van
    directo; las demás siguen pasando por la conexión.
    """
    con_su_base = app.call_tool
    directo = type(app).call_tool.__get__(app)

    async def call_tool(name, arguments, context=None):
        if name.startswith('wansoft_'):
            return await directo(name, arguments, context)
        return await con_su_base(name, arguments, context)

    app.call_tool = call_tool


class ClaveDelEnlace:
    """Middleware ASGI: atiende solo /<clave>/mcp y responde /health.

    Compara la huella SHA-256 de la clave en tiempo constante. Lo que no
    coincide responde 404, igual que una ruta inexistente: no se distingue
    «clave equivocada» de «acá no hay nada».
    """

    def __init__(self, app, huella_clave: str):
        if len(huella_clave or '') != 64:
            raise ValueError('MCP_LINK_SECRET_SHA256 tiene que ser una huella SHA-256 en hexadecimal.')
        self.app = app
        self.huella = huella_clave.lower()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            return await self.app(scope, receive, send)
        if scope['type'] != 'http':
            return await self._responder(send, 404, {'error': 'No encontrado'})

        ruta = scope.get('path', '')
        if ruta == '/health':
            return await self._responder(send, 200, {'status': 'ok'})

        partes = ruta.split('/', 2)  # '', <clave>, 'mcp...'
        if len(partes) == 3 and (partes[2] == RUTA_MCP[1:] or partes[2].startswith(RUTA_MCP[1:] + '/')):
            huella = hashlib.sha256(partes[1].encode()).hexdigest()
            if hmac.compare_digest(huella, self.huella):
                resto = '/' + partes[2]
                scope = dict(scope, path=resto, raw_path=resto.encode())
                return await self.app(scope, receive, send)
        return await self._responder(send, 404, {'error': 'No encontrado'})

    @staticmethod
    async def _responder(send, status, datos):
        cuerpo = json.dumps(datos).encode()
        await send({'type': 'http.response.start', 'status': status,
                    'headers': [(b'content-type', b'application/json'),
                                (b'content-length', str(len(cuerpo)).encode())]})
        await send({'type': 'http.response.body', 'body': cuerpo})


def armar_app(servidor: OdooMCPServer):
    starlette = servidor.app.streamable_http_app(
        streamable_http_path=RUTA_MCP,
        transport_security=servidor._transport_security,
        session_idle_timeout=servidor.config.session_idle_timeout,
        host=servidor.config.host,
    )
    return ClaveDelEnlace(starlette, os.environ['MCP_LINK_SECRET_SHA256'])
