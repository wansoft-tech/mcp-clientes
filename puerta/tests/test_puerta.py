import base64
import contextlib
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx

from puerta_wansoft.app import Config, armar
from puerta_wansoft.firmas import clave_del_enlace, firma_hex, firmar, leer

URL = 'https://clientes.mcp.example'
ODOO = 'https://odoo.example'
CALLBACK = 'https://claude.ai/api/mcp/auth_callback'
SECRETO_ODOO = b'o' * 40
HOST = 'c-7.mcp.example'


class Mundo:
    """Odoo y el servidor de la persona, de mentira."""

    def __init__(self):
        self.activa = True
        self.servidor_existe = True
        self.pedidos = []

    def __call__(self, request: httpx.Request):
        self.pedidos.append(request)
        if request.url.host == 'odoo.example':
            fecha = request.headers['x-wansoft-fecha']
            assert request.headers['x-wansoft-firma'] == firma_hex(SECRETO_ODOO, fecha, request.content)
            return httpx.Response(200, json={'activa': self.activa, 'host': HOST if self.activa else None})
        assert request.url.host == HOST
        if not self.servidor_existe:
            return httpx.Response(404, json={'error': 'No encontrado'})
        assert request.url.path == '/%s/mcp' % clave_del_enlace(SECRETO_ODOO, 7)
        assert 'authorization' not in request.headers
        cuerpo = json.loads(request.content)
        return httpx.Response(200, headers={'content-type': 'application/json', 'mcp-session-id': 's1'},
                              json={'jsonrpc': '2.0', 'id': cuerpo.get('id'), 'result': {'ok': True}})


def entorno(tmp_path):
    return {'PUERTA_URL': URL, 'PUERTA_SECRETO': 'x' * 40, 'ODOO_URL': ODOO,
            'PUERTA_ODOO_SECRETO': SECRETO_ODOO.decode(), 'PUERTA_CLIENTES': str(tmp_path / 'c.json')}


@contextlib.asynccontextmanager
async def abrir(tmp_path, mundo):
    app = armar(Config(entorno(tmp_path)), transporte=httpx.MockTransport(mundo))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as c:
        yield c


async def iniciar_sesion(c, conexion=7):
    r = await c.post('/register', json={'redirect_uris': [CALLBACK], 'client_name': 'Claude',
                                         'token_endpoint_auth_method': 'none',
                                         'grant_types': ['authorization_code', 'refresh_token'],
                                         'response_types': ['code']})
    client_id = r.json()['client_id']
    verificador = secrets.token_urlsafe(48)
    desafio = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b'=').decode()
    r = await c.get('/authorize', params={
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': CALLBACK,
        'code_challenge': desafio, 'code_challenge_method': 'S256', 'state': 'abc'})
    destino = urlparse(r.headers['location'])
    assert destino.geturl().startswith(ODOO + '/my/claude/autorizar?')
    pedido = parse_qs(destino.query)['pedido'][0]
    # Lo que hace Odoo después del inicio de sesión:
    respuesta = firmar(SECRETO_ODOO, 'respuesta', {
        'ph': hashlib.sha256(pedido.encode()).hexdigest(), 'con': conexion, 'h': HOST}, 300)
    r = await c.get('/volver', params={'pedido': pedido, 'respuesta': respuesta})
    assert r.status_code == 302, r.text
    vuelta = urlparse(r.headers['location'])
    assert vuelta.geturl().startswith(CALLBACK)
    codigo = parse_qs(vuelta.query)['code'][0]
    r = await c.post('/token', data={'grant_type': 'authorization_code', 'code': codigo,
                                     'redirect_uri': CALLBACK, 'client_id': client_id,
                                     'code_verifier': verificador})
    assert r.status_code == 200, r.text
    return client_id, r.json(), pedido


def llamar(c, token, **extra):
    return c.post('/mcp', headers={'Authorization': 'Bearer %s' % token, 'Accept': 'application/json, text/event-stream',
                                   'Content-Type': 'application/json', **extra},
                  json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})


def test_firmas_y_clave():
    s = b'y' * 40
    assert leer(s, 'acceso', firmar(s, 'acceso', {'a': 1}, 60))['a'] == 1
    assert leer(s, 'codigo', firmar(s, 'acceso', {}, 60)) is None
    assert clave_del_enlace(s, 7) == clave_del_enlace(s, 7) != clave_del_enlace(s, 8)
    assert len(clave_del_enlace(s, 7)) == 43


def test_secretos_distintos(tmp_path):
    datos = dict(entorno(tmp_path), PUERTA_ODOO_SECRETO='x' * 40)
    try:
        Config(datos)
    except ValueError:
        return
    raise AssertionError('tenía que fallar')


async def test_descubrimiento_y_sin_credencial(tmp_path):
    async with abrir(tmp_path, Mundo()) as c:
        assert (await c.get('/.well-known/oauth-authorization-server')).json()['issuer'] == URL
        r = await c.get('/.well-known/oauth-protected-resource/mcp')
        assert r.json()['resource'] == URL + '/mcp'
        r = await c.post('/mcp', json={})
        assert r.status_code == 401
        assert 'resource_metadata' in r.headers['www-authenticate']


async def test_inicio_de_sesion_y_reenvio(tmp_path):
    mundo = Mundo()
    async with abrir(tmp_path, mundo) as c:
        _cid, credenciales, _ = await iniciar_sesion(c)
        assert credenciales['expires_in'] == 3600
        r = await llamar(c, credenciales['access_token'])
        assert r.status_code == 200, r.text
        assert r.json()['result'] == {'ok': True}
        assert r.headers['mcp-session-id'] == 's1'
        assert mundo.pedidos[-1].headers['host'] == HOST


async def test_volver_rechaza_respuestas_ajenas(tmp_path):
    async with abrir(tmp_path, Mundo()) as c:
        _cid, _cred, pedido = await iniciar_sesion(c)
        otra = firmar(SECRETO_ODOO, 'respuesta', {'ph': 'otro', 'con': 7, 'h': HOST}, 300)
        assert (await c.get('/volver', params={'pedido': pedido, 'respuesta': otra})).status_code == 400
        falsa = firmar(b'z' * 40, 'respuesta', {'ph': hashlib.sha256(pedido.encode()).hexdigest(),
                                                'con': 7, 'h': HOST}, 300)
        assert (await c.get('/volver', params={'pedido': pedido, 'respuesta': falsa})).status_code == 400


async def test_renovacion_pregunta_a_odoo(tmp_path):
    mundo = Mundo()
    async with abrir(tmp_path, mundo) as c:
        client_id, credenciales, _ = await iniciar_sesion(c)
        datos = {'grant_type': 'refresh_token', 'refresh_token': credenciales['refresh_token'], 'client_id': client_id}
        r = await c.post('/token', data=datos)
        assert r.status_code == 200, r.text
        assert any(p.url.host == 'odoo.example' for p in mundo.pedidos)
        mundo.activa = False
        r = await c.post('/token', data=datos)
        assert r.status_code == 400
        assert r.json()['error'] == 'invalid_grant'


async def test_sin_servidor_pide_iniciar_sesion_de_nuevo(tmp_path):
    mundo = Mundo()
    async with abrir(tmp_path, mundo) as c:
        _cid, credenciales, _ = await iniciar_sesion(c)
        mundo.servidor_existe = False
        r = await llamar(c, credenciales['access_token'])
        assert r.status_code == 401
        assert 'resource_metadata' in r.headers['www-authenticate']


async def test_la_renovacion_no_sirve_de_credencial(tmp_path):
    async with abrir(tmp_path, Mundo()) as c:
        _cid, credenciales, _ = await iniciar_sesion(c)
        assert (await llamar(c, credenciales['refresh_token'])).status_code == 401


async def test_registro_sobrevive_al_reinicio(tmp_path):
    async with abrir(tmp_path, Mundo()) as c:
        client_id, _cred, _ = await iniciar_sesion(c)
    otra = armar(Config(entorno(tmp_path)))
    assert await otra.state.proveedor.get_client(client_id) is not None
