"""Prueba de concepto de la puerta del conector «Wansoft» (diseño §9 ter).

Lo único que prueba es si claude.ai completa con esta puerta el registro
dinámico, el inicio de sesión, el canje del código y la renovación de la
credencial. No toca ninguna base de Odoo ni ningún servidor de clientes.

- El inicio de sesión es una página con una contraseña fija (PUERTA_POC_CLAVE).
  En el diseño real, acá va el portal de Wansoft.
- Códigos, credenciales y renovaciones son datos firmados: la puerta no los
  guarda. Los clientes registrados se guardan en un archivo, si hay volumen.
- La credencial dura poco (PUERTA_POC_SEGUNDOS, 5 minutos por defecto) para
  ver la renovación en una sesión de prueba.
- Una sola herramienta, quien_soy, que dice qué credencial llegó.
"""
import hmac
import html
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .firmas import firmar, leer

CODIGO_SEGUNDOS = 300
RENOVACION_SEGUNDOS = 30 * 24 * 3600
PEDIDO_SEGUNDOS = 600


class Config:
    def __init__(self, entorno=os.environ):
        self.url = entorno['PUERTA_URL'].rstrip('/')  # https://poc.mcp.wansoft.tech
        self.secreto = entorno['PUERTA_SECRETO'].encode()
        if len(self.secreto) < 32:
            raise ValueError('PUERTA_SECRETO tiene que tener al menos 32 caracteres.')
        self.clave = entorno['PUERTA_POC_CLAVE']
        self.segundos = int(entorno.get('PUERTA_POC_SEGUNDOS', '300'))
        self.archivo = Path(entorno.get('PUERTA_CLIENTES', '/data/clientes.json'))


class Proveedor:
    """El servidor de autorización. Implementa el protocolo del SDK de MCP."""

    def __init__(self, config: Config):
        self.config = config
        self.clientes: dict[str, OAuthClientInformationFull] = {}
        self.eventos: list[str] = []
        self._cargar()

    # Registro, con archivo si hay volumen ----------------------------
    def _cargar(self):
        try:
            datos = json.loads(self.config.archivo.read_text())
        except (OSError, ValueError):
            return
        for valor in datos.values():
            cliente = OAuthClientInformationFull.model_validate(valor)
            self.clientes[cliente.client_id] = cliente

    def _guardar(self):
        try:
            self.config.archivo.parent.mkdir(parents=True, exist_ok=True)
            self.config.archivo.write_text(json.dumps(
                {k: v.model_dump(mode='json') for k, v in self.clientes.items()}))
        except OSError:
            pass

    def anotar(self, texto):
        self.eventos = (self.eventos + ['%s %s' % (datetime.now(timezone.utc).strftime('%H:%M:%S'), texto)])[-50:]

    async def get_client(self, client_id):
        return self.clientes.get(client_id)

    async def register_client(self, client_info):
        self.clientes[client_info.client_id] = client_info
        self._guardar()
        self.anotar('registro: %s %s' % (client_info.client_name, [str(u) for u in client_info.redirect_uris or []]))

    # Autorización ---------------------------------------------------
    async def authorize(self, client, params: AuthorizationParams):
        pedido = firmar(self.config.secreto, 'pedido', {
            'c': client.client_id,
            'st': params.state,
            'sc': params.scopes or [],
            'cc': params.code_challenge,
            'ru': str(params.redirect_uri),
            'rx': params.redirect_uri_provided_explicitly,
            'rs': params.resource,
        }, PEDIDO_SEGUNDOS)
        self.anotar('autorizar: %s' % client.client_name)
        return '%s/entrar?pedido=%s' % (self.config.url, pedido)

    def emitir_codigo(self, pedido: dict, sujeto: str) -> str:
        codigo = firmar(self.config.secreto, 'codigo', {
            'c': pedido['c'], 'sc': pedido['sc'], 'cc': pedido['cc'], 'ru': pedido['ru'],
            'rx': pedido['rx'], 'rs': pedido['rs'], 'sub': sujeto,
        }, CODIGO_SEGUNDOS)
        return construct_redirect_uri(pedido['ru'], code=codigo, state=pedido['st'])

    async def load_authorization_code(self, client, authorization_code):
        datos = leer(self.config.secreto, 'codigo', authorization_code)
        if not datos or datos['c'] != client.client_id:
            self.anotar('código rechazado')
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=datos['sc'], expires_at=datos['exp'],
            client_id=datos['c'], code_challenge=datos['cc'], redirect_uri=datos['ru'],
            redirect_uri_provided_explicitly=datos['rx'], resource=datos['rs'], subject=datos['sub'])

    def _credenciales(self, client_id, scopes, resource, sujeto) -> OAuthToken:
        base = {'c': client_id, 'sc': scopes, 'rs': resource, 'sub': sujeto}
        return OAuthToken(
            access_token=firmar(self.config.secreto, 'acceso', base, self.config.segundos),
            token_type='Bearer',
            expires_in=self.config.segundos,
            refresh_token=firmar(self.config.secreto, 'renovar', base, RENOVACION_SEGUNDOS),
            scope=' '.join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code):
        self.anotar('canje del código: %s' % authorization_code.subject)
        return self._credenciales(client.client_id, authorization_code.scopes,
                                  authorization_code.resource, authorization_code.subject)

    async def load_refresh_token(self, client, refresh_token):
        datos = leer(self.config.secreto, 'renovar', refresh_token)
        if not datos or datos['c'] != client.client_id:
            self.anotar('renovación rechazada')
            return None
        return RefreshToken(token=refresh_token, client_id=datos['c'], scopes=datos['sc'],
                            expires_at=datos['exp'], resource=datos['rs'], subject=datos['sub'])

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # En el diseño real, acá se le pregunta a Odoo si la conexión sigue activa.
        self.anotar('renovación: %s' % refresh_token.subject)
        return self._credenciales(client.client_id, scopes or refresh_token.scopes,
                                  refresh_token.resource, refresh_token.subject)

    async def load_access_token(self, token):
        datos = leer(self.config.secreto, 'acceso', token)
        if not datos:
            return None
        return AccessToken(token=token, client_id=datos['c'], scopes=datos['sc'],
                           expires_at=datos['exp'], resource=datos['rs'], subject=datos['sub'])

    async def revoke_token(self, token):
        self.anotar('revocación pedida (sin efecto en la prueba)')

    async def exchange_identity_assertion(self, client, params):
        raise TokenError('unsupported_grant_type', 'No se usa.')


PAGINA = '''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Wansoft · Conectá con Claude</title>
<style>body{font-family:system-ui,sans-serif;max-width:420px;margin:48px auto;padding:0 16px;color:#1f2a44}
input,button{font-size:16px;padding:10px;width:100%%;box-sizing:border-box;margin-top:8px}
button{background:#374874;color:#fff;border:0;border-radius:6px}.error{color:#b00020}</style></head>
<body><h1>Conectá con Claude</h1><p>Prueba de concepto de Wansoft. Ingresá la clave de la prueba.</p>
%s<form method="post"><input type="hidden" name="pedido" value="%s">
<input type="password" name="clave" placeholder="Clave de la prueba" autofocus required>
<button type="submit">Entrar</button></form></body></html>'''


def armar(config: Config | None = None):
    config = config or Config()
    proveedor = Proveedor(config)
    servidor = MCPServer(
        name='Wansoft',
        instructions='Prueba de concepto del conector Wansoft. La única herramienta, quien_soy, '
                     'dice con qué credencial llegó el pedido.',
        auth_server_provider=proveedor,
        auth=AuthSettings(
            issuer_url=config.url,
            resource_server_url=config.url + '/mcp',
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @servidor.tool()
    def quien_soy() -> dict:
        """Dice quién entró, con qué cliente y cuándo vence la credencial."""
        token = get_access_token()
        if token is None:
            return {'error': 'sin credencial'}
        return {
            'persona': token.subject,
            'cliente': getattr(proveedor.clientes.get(token.client_id), 'client_name', token.client_id),
            'vence_en_segundos': (token.expires_at or 0) - int(time.time()),
            'hora_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }

    @servidor.custom_route('/entrar', methods=['GET', 'POST'])
    async def entrar(request: Request):
        if request.method == 'GET':
            pedido = request.query_params.get('pedido', '')
            if not leer(config.secreto, 'pedido', pedido):
                return HTMLResponse('El pedido venció. Volvé a Claude y tocá Conectar de nuevo.', 400)
            return HTMLResponse(PAGINA % ('', html.escape(pedido)))
        formulario = await request.form()
        texto = str(formulario.get('pedido', ''))
        pedido = leer(config.secreto, 'pedido', texto)
        if not pedido:
            return HTMLResponse('El pedido venció. Volvé a Claude y tocá Conectar de nuevo.', 400)
        if not hmac.compare_digest(str(formulario.get('clave', '')).encode(), config.clave.encode()):
            proveedor.anotar('clave equivocada')
            return HTMLResponse(PAGINA % ('<p class="error">Clave equivocada.</p>', html.escape(texto)), 401)
        proveedor.anotar('inicio de sesión correcto')
        return RedirectResponse(proveedor.emitir_codigo(pedido, 'persona-de-prueba'), 302)

    @servidor.custom_route('/health', methods=['GET'])
    async def salud(request: Request):
        return JSONResponse({'status': 'ok'})

    @servidor.custom_route('/poc/eventos', methods=['GET'])
    async def eventos(request: Request):
        # Lo que pasó, sin datos: para seguir la prueba desde afuera. Pide la clave.
        if not hmac.compare_digest(request.headers.get('x-clave', '').encode(), config.clave.encode()):
            return JSONResponse({'error': 'No encontrado'}, 404)
        return JSONResponse({'eventos': proveedor.eventos})

    host = config.url.split('://', 1)[1]
    from mcp.server.transport_security import TransportSecuritySettings
    seguridad = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                          allowed_hosts=[host, host + ':*'],
                                          allowed_origins=['https://claude.ai', 'https://claude.com'])
    return servidor, servidor.streamable_http_app(transport_security=seguridad, host='0.0.0.0'), proveedor
