"""Arma el servidor: mcp-server-odoo con lo que su perfil y su nivel permiten, detrás de la clave del enlace.

Sobre mcp-server-odoo 0.9.0, sin modificar sus archivos. Dos perfiles, por
la variable MCP_PERFIL:

- «cliente» (por defecto): una persona del cliente. Su nivel, MCP_NIVEL, lo
  elige el líder del proyecto: lectura (por defecto), escritura o métodos.
  Borrar, nunca. Suma las herramientas de Wansoft.
- «equipo»: el equipo de Wansoft sobre una base del cliente. Todo, borrar
  incluido, y sin herramientas de Wansoft.

1. Lo que el nivel no permite no existe en el servidor: se sacan todas las
   herramientas que no están en su lista de permitidas. Una lista de
   permitidas y no de prohibidas: una herramienta nueva de una versión futura
   del original queda afuera hasta que alguien la sume a propósito.
2. Las herramientas de Wansoft, con prefijo wansoft_, solo en el perfil cliente.
3. Las instrucciones para Claude, en español.
4. La clave del enlace: solo se atiende /<clave>/mcp. Cualquier otra ruta o
   clave responde 404 sin tocar Odoo. /health queda abierto y no dice nada.
"""
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request

from mcp_server_odoo.config import load_config
from mcp_server_odoo.server import OdooMCPServer
from mcp_server_odoo.user_context import usage_guidance

from . import wansoft
from .instrucciones import INSTRUCCIONES_EQUIPO, instrucciones_cliente

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

# Crear y modificar. Borrar (delete_record) no está: para el cliente, nunca.
ESCRITURA = LECTURA | frozenset({
    'create_record',
    'create_records',
    'update_record',
    'update_records',
    'post_message',
    'upload_attachment',
})
METODOS = ESCRITURA | frozenset({'call_model_method'})
EQUIPO = METODOS | frozenset({'delete_record'})

NIVELES = {'lectura': LECTURA, 'escritura': ESCRITURA, 'metodos': METODOS}

RUTA_MCP = '/mcp'


def perfil_y_nivel(entorno=os.environ):
    """El perfil y el nivel pedidos. Un valor desconocido no abre nada: falla."""
    perfil = entorno.get('MCP_PERFIL', 'cliente')
    if perfil == 'equipo':
        return perfil, 'equipo'
    if perfil != 'cliente':
        raise ValueError('MCP_PERFIL tiene que ser cliente o equipo.')
    nivel = entorno.get('MCP_NIVEL', 'lectura')
    if nivel not in NIVELES:
        raise ValueError('MCP_NIVEL tiene que ser lectura, escritura o metodos.')
    return perfil, nivel


def configurar_original(nivel, entorno=os.environ) -> None:
    """Lo que el original necesita para registrar las herramientas del nivel.

    El original solo registra las de escritura con ODOO_YOLO=true y la de
    métodos con ODOO_MCP_ENABLE_METHOD_CALLS. Lo que el nivel no permite se
    saca después igual.
    """
    entorno['ODOO_MCP_TRANSPORT'] = 'streamable-http'
    if nivel == 'lectura':
        entorno['ODOO_YOLO'] = 'read'
        entorno.pop('ODOO_MCP_ENABLE_METHOD_CALLS', None)
    elif nivel == 'escritura':
        entorno['ODOO_YOLO'] = 'true'
        entorno.pop('ODOO_MCP_ENABLE_METHOD_CALLS', None)
    else:
        entorno['ODOO_YOLO'] = 'true'
        entorno['ODOO_MCP_ENABLE_METHOD_CALLS'] = 'true'


def detectar_base(entorno=os.environ, timeout=15) -> None:
    """Completa ODOO_DB si viene vacío o en «auto», preguntándole a su Odoo.

    Odoo.sh cambia el nombre de la base de staging cada vez que arma una copia
    nueva. /web/database/list responde aunque la lista por XML-RPC esté
    cerrada: si hay una sola base, es esa. Así un servidor de staging se
    reinicia y sigue andando sin que nadie cambie nada.
    """
    if (entorno.get('ODOO_DB') or '').strip().lower() not in ('', 'auto'):
        return
    url = entorno['ODOO_URL'].rstrip('/') + '/web/database/list'
    cuerpo = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {}}).encode()
    pedido = urllib.request.Request(url, data=cuerpo, method='POST',
                                    headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
            bases = (json.loads(respuesta.read()) or {}).get('result') or []
    except (OSError, ValueError):
        bases = []
    # Odoo Online: si no informa la lista, la base se llama como el subdominio
    # (cliente-prueba.odoo.com → cliente-prueba).
    host = urllib.parse.urlparse(entorno['ODOO_URL']).hostname or ''
    if not bases and host.endswith('.odoo.com') and host.count('.') == 2:
        bases = [host.split('.')[0]]
    if len(bases) != 1:
        raise ValueError('No se pudo detectar la base de %s: hay %d. Cargá ODOO_DB.'
                         % (entorno['ODOO_URL'], len(bases)))
    entorno['ODOO_DB'] = bases[0]


def forzar_solo_lectura(entorno=os.environ) -> None:
    configurar_original('lectura', entorno)


def armar_servidor() -> OdooMCPServer:
    perfil, nivel = perfil_y_nivel()
    configurar_original(nivel)
    detectar_base()
    # load_config y no get_config: este lee el entorno una sola vez y lo
    # guarda, y el nivel se acaba de fijar en el entorno.
    config = load_config()
    servidor = OdooMCPServer(config)
    app = servidor.app

    permitidas = EQUIPO if perfil == 'equipo' else NIVELES[nivel]
    for herramienta in list(app._tool_manager.list_tools()):
        if herramienta.name not in permitidas:
            app.remove_tool(herramienta.name)

    if perfil == 'cliente':
        api = wansoft.ApiWansoft(os.environ['WANSOFT_API_URL'], os.environ['WANSOFT_TOKEN'])
        wansoft.registrar(app, api)
        separar_wansoft_de_su_base(app)
        instrucciones = instrucciones_cliente(nivel)
    else:
        instrucciones = INSTRUCCIONES_EQUIPO

    # El original personaliza las instrucciones con el usuario conectado a
    # partir de _static_instructions: se reemplaza esa base.
    uso = usage_guidance(h.name for h in app._tool_manager.list_tools())
    servidor._static_instructions = '%s\n\n%s' % (instrucciones, uso) if uso else instrucciones
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
