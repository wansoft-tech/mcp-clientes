"""La puerta del conector «Wansoft» (diseño §9 ter de docs/mcp-soporte-clientes.md).

Un solo conector para todos los clientes. La persona entra con su usuario del
portal de Wansoft y Claude sabe quién es por el inicio de sesión, no por el
link. La puerta:

1. Es el servidor de autorización OAuth 2.1 que pide claude.ai: registro
   dinámico, PKCE, código, credencial y renovación. Sobre el SDK de MCP.
2. Manda a la persona a iniciar sesión al portal de Wansoft
   (/my/claude/autorizar). Si no tiene conexión, ahí hace el alta con su clave
   de API. Odoo la devuelve a /volver con una respuesta firmada que dice qué
   conexión es.
3. Reenvía cada pedido de Claude al servidor de esa persona, con la clave del
   enlace derivada del secreto compartido con Odoo.

No guarda códigos ni credenciales: los firma. La credencial dura una hora. La
renovación le pregunta a Odoo si la conexión sigue activa y da un plazo nuevo
de 90 días. Una baja elimina el servidor de la persona: el próximo pedido ya
no tiene adónde ir y Claude vuelve a pedir el inicio de sesión.
"""
import hashlib
import html
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx2 as httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    ProviderTokenVerifier,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import build_resource_metadata_url, create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .firmas import clave_del_enlace, firma_hex, firmar, leer

ACCESO_SEGUNDOS = 3600
RENOVACION_SEGUNDOS = 90 * 24 * 3600
CODIGO_SEGUNDOS = 300
PEDIDO_SEGUNDOS = 900

# Cabeceras que pasan de Claude al servidor de la persona y de vuelta.
HACIA_SERVIDOR = ('accept', 'content-type', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id')
HACIA_CLAUDE = ('content-type', 'mcp-session-id', 'cache-control')


class Config:
    def __init__(self, entorno=os.environ):
        self.url = entorno['PUERTA_URL'].rstrip('/')          # https://clientes.mcp.wansoft.tech
        self.secreto = entorno['PUERTA_SECRETO'].encode()     # firma lo que emite la puerta
        self.odoo_url = entorno['ODOO_URL'].rstrip('/')       # el Odoo de Wansoft
        self.odoo_secreto = entorno['PUERTA_ODOO_SECRETO'].encode()  # el mismo que en Odoo
        for nombre, valor in (('PUERTA_SECRETO', self.secreto), ('PUERTA_ODOO_SECRETO', self.odoo_secreto)):
            if len(valor) < 32:
                raise ValueError('%s tiene que tener al menos 32 caracteres.' % nombre)
        if self.secreto == self.odoo_secreto:
            raise ValueError('PUERTA_SECRETO y PUERTA_ODOO_SECRETO tienen que ser distintos.')
        # Adónde reenviar: el servidor de la persona por su dominio público, o
        # por la red interna de Easypanel (http://clientes-mcp_{servicio}:8000).
        self.destino = entorno.get('PUERTA_DESTINO', 'https://{host}')
        self.archivo = Path(entorno.get('PUERTA_CLIENTES', '/data/clientes.json'))


class Codigo(AuthorizationCode):
    host: str


class ErrorOdoo(Exception):
    pass


class Odoo:
    """Las dos cosas que la puerta le pregunta a Odoo, firmadas."""

    def __init__(self, config: Config, cliente: httpx.AsyncClient):
        self.config = config
        self.cliente = cliente

    async def conexion(self, conexion_id: int) -> dict:
        cuerpo = json.dumps({'conexion': conexion_id}).encode()
        fecha = str(int(time.time()))
        try:
            respuesta = await self.cliente.post(
                self.config.odoo_url + '/wansoft/mcp/v1/puerta/conexion', content=cuerpo,
                headers={'Content-Type': 'application/json', 'X-Wansoft-Fecha': fecha,
                         'X-Wansoft-Firma': firma_hex(self.config.odoo_secreto, fecha, cuerpo)},
                timeout=15)
        except httpx.HTTPError as error:
            raise ErrorOdoo(str(error)) from None
        if respuesta.status_code != 200:
            raise ErrorOdoo('HTTP %s' % respuesta.status_code)
        return respuesta.json()


class Proveedor:
    """El servidor de autorización: el protocolo OAuthAuthorizationServerProvider del SDK."""

    def __init__(self, config: Config, odoo: Odoo):
        self.config = config
        self.odoo = odoo
        self.clientes: dict[str, OAuthClientInformationFull] = {}
        self._cargar()

    # Los clientes registrados (claude.ai) se guardan en el volumen: si se
    # pierden, claude.ai se vuelve a registrar solo.
    def _cargar(self):
        try:
            datos = json.loads(self.config.archivo.read_text())
        except (OSError, ValueError):
            return
        for valor in datos.values():
            try:
                cliente = OAuthClientInformationFull.model_validate(valor)
            except ValueError:
                continue
            self.clientes[cliente.client_id] = cliente

    def _guardar(self):
        try:
            self.config.archivo.parent.mkdir(parents=True, exist_ok=True)
            temporal = self.config.archivo.with_suffix('.tmp')
            temporal.write_text(json.dumps({k: v.model_dump(mode='json') for k, v in self.clientes.items()}))
            temporal.replace(self.config.archivo)
        except OSError:
            pass

    async def get_client(self, client_id):
        return self.clientes.get(client_id)

    async def register_client(self, client_info):
        self.clientes[client_info.client_id] = client_info
        self._guardar()

    async def authorize(self, client, params: AuthorizationParams):
        pedido = firmar(self.config.secreto, 'pedido', {
            'c': client.client_id, 'st': params.state, 'sc': params.scopes or [],
            'cc': params.code_challenge, 'ru': str(params.redirect_uri),
            'rx': params.redirect_uri_provided_explicitly, 'rs': params.resource,
        }, PEDIDO_SEGUNDOS)
        return '%s/my/claude/autorizar?%s' % (self.config.odoo_url, urlencode({'pedido': pedido}))

    def emitir_codigo(self, pedido: dict, conexion_id: int, host: str) -> str:
        codigo = firmar(self.config.secreto, 'codigo', {
            'c': pedido['c'], 'sc': pedido['sc'], 'cc': pedido['cc'], 'ru': pedido['ru'],
            'rx': pedido['rx'], 'rs': pedido['rs'], 'sub': str(conexion_id), 'h': host,
        }, CODIGO_SEGUNDOS)
        return construct_redirect_uri(pedido['ru'], code=codigo, state=pedido['st'])

    async def load_authorization_code(self, client, authorization_code):
        datos = leer(self.config.secreto, 'codigo', authorization_code)
        if not datos or datos['c'] != client.client_id:
            return None
        return Codigo(
            code=authorization_code, scopes=datos['sc'], expires_at=datos['exp'],
            client_id=datos['c'], code_challenge=datos['cc'], redirect_uri=datos['ru'],
            redirect_uri_provided_explicitly=datos['rx'], resource=datos['rs'], subject=datos['sub'],
            host=datos['h'])

    def _credenciales(self, client_id, scopes, resource, sujeto, host) -> OAuthToken:
        base = {'c': client_id, 'sc': scopes, 'rs': resource, 'sub': sujeto, 'h': host}
        return OAuthToken(
            access_token=firmar(self.config.secreto, 'acceso', base, ACCESO_SEGUNDOS),
            token_type='Bearer',
            expires_in=ACCESO_SEGUNDOS,
            refresh_token=firmar(self.config.secreto, 'renovar', base, RENOVACION_SEGUNDOS),
            scope=' '.join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code):
        return self._credenciales(client.client_id, authorization_code.scopes, authorization_code.resource,
                                  authorization_code.subject, authorization_code.host)

    async def load_refresh_token(self, client, refresh_token):
        datos = leer(self.config.secreto, 'renovar', refresh_token)
        if not datos or datos['c'] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=datos['c'], scopes=datos['sc'],
                            expires_at=datos['exp'], resource=datos['rs'], subject=datos['sub'])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        """Renueva solo si la conexión sigue activa en Odoo, con el servidor que tenga hoy."""
        try:
            estado = await self.odoo.conexion(int(refresh_token.subject))
        except ErrorOdoo:
            raise TokenError('temporarily_unavailable', 'Wansoft no responde. Probá en unos minutos.') from None
        if not estado.get('activa') or not estado.get('host'):
            raise TokenError('invalid_grant', 'La conexión ya no está activa. Volvé a conectar el conector.')
        return self._credenciales(client.client_id, scopes or refresh_token.scopes, refresh_token.resource,
                                  refresh_token.subject, estado['host'])

    async def load_access_token(self, token):
        datos = leer(self.config.secreto, 'acceso', token)
        if not datos:
            return None
        return AccessToken(token=token, client_id=datos['c'], scopes=datos['sc'], expires_at=datos['exp'],
                           resource=datos['rs'], subject=datos['sub'], claims={'host': datos['h']})

    async def revoke_token(self, token):
        # Nada que borrar: la baja se hace desde el portal y elimina el servidor.
        return None

    async def exchange_identity_assertion(self, client, params):
        raise TokenError('unsupported_grant_type', 'No se usa.')


def _pagina(titulo, texto, estado=400):
    return HTMLResponse(
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Wansoft</title>'
        '<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:48px auto;padding:0 16px;'
        'color:#1f2a44}</style></head><body><h1>%s</h1><p>%s</p></body></html>'
        % (html.escape(titulo), html.escape(texto)), estado)


def armar(config: Config | None = None, transporte=None):
    """La app ASGI. `transporte` es para los tests: reemplaza la red."""
    config = config or Config()
    cliente = httpx.AsyncClient(transport=transporte, follow_redirects=False,
                                timeout=httpx.Timeout(connect=10, read=None, write=30, pool=10))
    odoo = Odoo(config, cliente)
    proveedor = Proveedor(config, odoo)
    # AuthSettings conserva la dirección sin barra final: el emisor se compara
    # como texto exacto, y una barra de más rompe el inicio de sesión.
    ajustes = AuthSettings(issuer_url=config.url, resource_server_url=config.url + '/mcp',
                           validate_token_resource=False)
    emisor, recurso = ajustes.issuer_url, ajustes.resource_server_url
    metadatos = build_resource_metadata_url(recurso)

    async def volver(request: Request):
        """Odoo devuelve a la persona acá, con la conexión firmada."""
        texto = request.query_params.get('pedido', '')
        pedido = leer(config.secreto, 'pedido', texto)
        respuesta = leer(config.odoo_secreto, 'respuesta', request.query_params.get('respuesta', ''))
        if not pedido or not respuesta:
            return _pagina('El pedido venció', 'Volvé a Claude y tocá «Conectar» de nuevo.')
        if respuesta.get('ph') != hashlib.sha256(texto.encode()).hexdigest():
            return _pagina('Pedido inválido', 'Volvé a Claude y tocá «Conectar» de nuevo.')
        if not await proveedor.get_client(pedido['c']):
            return _pagina('Pedido inválido', 'Volvé a Claude y tocá «Conectar» de nuevo.')
        return RedirectResponse(proveedor.emitir_codigo(pedido, int(respuesta['con']), respuesta['h']), 302)

    async def reenviar(scope, receive, send):
        """Pasa el pedido de Claude al servidor de la persona y la respuesta de vuelta."""
        request = Request(scope, receive)
        usuario = scope.get('user')
        token = usuario.access_token if isinstance(usuario, AuthenticatedUser) else None
        host = (token.claims or {}).get('host') if token else None
        if not token or not host:
            return await JSONResponse({'error': 'invalid_token'}, 401)(scope, receive, send)
        servicio = host.split('.', 1)[0]
        clave = clave_del_enlace(config.odoo_secreto, int(token.subject))
        destino = config.destino.format(host=host, servicio=servicio).rstrip('/') + '/%s/mcp' % clave
        cabeceras = {k: v for k, v in request.headers.items() if k.lower() in HACIA_SERVIDOR}
        cabeceras['host'] = host
        cuerpo = await request.body()
        try:
            pedido = cliente.build_request(request.method, destino, headers=cabeceras,
                                           content=cuerpo or None)
            respuesta = await cliente.send(pedido, stream=True)
        except httpx.HTTPError:
            error = JSONResponse({'error': 'Tu servicio no responde. Probá en unos minutos.'}, 503)
            return await error(scope, receive, send)
        if respuesta.status_code == 404 and request.method == 'POST' and not request.headers.get('mcp-session-id'):
            # Sin servidor: la conexión se dio de baja. Claude vuelve a pedir el
            # inicio de sesión, y el portal ofrece el alta.
            await respuesta.aclose()
            no = Response(status_code=401, headers={'WWW-Authenticate': (
                'Bearer error="invalid_token", error_description="La conexión ya no está activa", '
                'resource_metadata="%s"' % metadatos)})
            return await no(scope, receive, send)
        salida = StreamingResponse(
            respuesta.aiter_bytes(), status_code=respuesta.status_code,
            headers={k: v for k, v in respuesta.headers.items() if k.lower() in HACIA_CLAUDE},
            background=BackgroundTask(respuesta.aclose))
        return await salida(scope, receive, send)

    async def salud(request):
        return JSONResponse({'status': 'ok'})

    rutas = create_auth_routes(
        provider=proveedor, issuer_url=emisor,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    rutas += create_protected_resource_routes(resource_url=recurso, authorization_servers=[emisor],
                                              resource_name='Wansoft')
    rutas += [
        Route('/mcp', endpoint=RequireAuthMiddleware(reenviar, [], metadatos)),
        Route('/volver', endpoint=volver, methods=['GET']),
        Route('/health', endpoint=salud, methods=['GET']),
    ]
    app = Starlette(routes=rutas, middleware=[
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(ProviderTokenVerifier(proveedor))),
        Middleware(AuthContextMiddleware),
    ])
    app.state.proveedor = proveedor
    return app
