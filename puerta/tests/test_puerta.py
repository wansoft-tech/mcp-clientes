import base64
import contextlib
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx2 as httpx
import pytest

from puerta_wansoft.app import Config, armar
from puerta_wansoft.firmas import firmar, leer

URL = 'https://poc.mcp.example'
CALLBACK = 'https://claude.ai/api/mcp/auth_callback'


@pytest.fixture
def entorno(tmp_path):
    return {'PUERTA_URL': URL, 'PUERTA_SECRETO': 'x' * 40, 'PUERTA_POC_CLAVE': 'clave-poc',
            'PUERTA_CLIENTES': str(tmp_path / 'clientes.json')}


@contextlib.asynccontextmanager
async def abrir(entorno):
    servidor, app, proveedor = armar(Config(entorno))
    async with servidor.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as c:
            yield c


def test_firmas():
    s = b'y' * 40
    texto = firmar(s, 'acceso', {'sub': 'a'}, 60)
    assert leer(s, 'acceso', texto)['sub'] == 'a'
    assert leer(s, 'codigo', texto) is None
    assert leer(b'z' * 40, 'acceso', texto) is None
    assert leer(s, 'acceso', firmar(s, 'acceso', {}, -1)) is None
    assert leer(s, 'acceso', 'basura') is None


async def _registrar(c):
    r = await c.post('/register', json={'redirect_uris': [CALLBACK], 'client_name': 'Claude',
                                         'token_endpoint_auth_method': 'none',
                                         'grant_types': ['authorization_code', 'refresh_token'],
                                         'response_types': ['code']})
    assert r.status_code == 201, r.text
    return r.json()['client_id']


async def _flujo(c):
    client_id = await _registrar(c)
    verificador = secrets.token_urlsafe(48)
    desafio = base64.urlsafe_b64encode(hashlib.sha256(verificador.encode()).digest()).rstrip(b'=').decode()
    r = await c.get('/authorize', params={
        'response_type': 'code', 'client_id': client_id, 'redirect_uri': CALLBACK,
        'code_challenge': desafio, 'code_challenge_method': 'S256', 'state': 'abc',
        'resource': URL + '/mcp'})
    assert r.status_code == 302, r.text
    entrar = urlparse(r.headers['location'])
    assert entrar.path == '/entrar'
    pedido = parse_qs(entrar.query)['pedido'][0]
    assert (await c.get('/entrar', params={'pedido': pedido})).status_code == 200
    mala = await c.post('/entrar', data={'pedido': pedido, 'clave': 'otra'})
    assert mala.status_code == 401
    r = await c.post('/entrar', data={'pedido': pedido, 'clave': 'clave-poc'})
    assert r.status_code == 302
    vuelta = urlparse(r.headers['location'])
    assert vuelta.geturl().startswith(CALLBACK)
    q = parse_qs(vuelta.query)
    assert q['state'] == ['abc']
    r = await c.post('/token', data={'grant_type': 'authorization_code', 'code': q['code'][0],
                                     'redirect_uri': CALLBACK, 'client_id': client_id,
                                     'code_verifier': verificador})
    assert r.status_code == 200, r.text
    return client_id, r.json()


async def _llamar(c, token):
    h = {'Authorization': 'Bearer %s' % token, 'Accept': 'application/json, text/event-stream',
         'Content-Type': 'application/json'}
    r = await c.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}})
    assert r.status_code == 200, r.text
    h['mcp-session-id'] = r.headers['mcp-session-id']
    h['mcp-protocol-version'] = '2025-06-18'
    await c.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    r = await c.post('/mcp', headers=h, json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                              'params': {'name': 'quien_soy', 'arguments': {}}})
    assert r.status_code == 200, r.text
    linea = [l for l in r.text.splitlines() if l.startswith('data:')][-1] if 'data:' in r.text else r.text
    return json.loads(linea.removeprefix('data:').strip())


async def test_descubrimiento(entorno):
    async with abrir(entorno) as cliente:
        r = await cliente.get('/.well-known/oauth-authorization-server')
        assert r.status_code == 200
        datos = r.json()
        assert datos['registration_endpoint'] == URL + '/register'
        assert 'S256' in datos['code_challenge_methods_supported']
        r = await cliente.get('/.well-known/oauth-protected-resource/mcp')
        assert r.status_code == 200, r.text


async def test_sin_credencial_pide_iniciar_sesion(entorno):
    async with abrir(entorno) as cliente:
        r = await cliente.post('/mcp', json={})
        assert r.status_code == 401
        assert 'resource_metadata' in r.headers.get('www-authenticate', '')


async def test_flujo_completo_y_renovacion(entorno):
    async with abrir(entorno) as cliente:
        client_id, credenciales = await _flujo(cliente)
        assert credenciales['expires_in'] == 300
        respuesta = await _llamar(cliente, credenciales['access_token'])
        texto = json.dumps(respuesta)
        assert 'persona-de-prueba' in texto and 'Claude' in texto
        r = await cliente.post('/token', data={'grant_type': 'refresh_token',
                                               'refresh_token': credenciales['refresh_token'],
                                               'client_id': client_id})
        assert r.status_code == 200, r.text
        assert 'persona-de-prueba' in json.dumps(await _llamar(cliente, r.json()['access_token']))


async def test_un_codigo_no_sirve_de_credencial(entorno):
    async with abrir(entorno) as cliente:
        _client_id, credenciales = await _flujo(cliente)
        r = await cliente.post('/mcp', headers={'Authorization': 'Bearer %s' % credenciales['refresh_token']},
                               json={})
        assert r.status_code == 401


async def test_registro_sobrevive_al_reinicio(entorno):
    servidor, app, _ = armar(Config(entorno))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=URL) as c:
        client_id = await _registrar(c)
    _s, _a, otro = armar(Config(entorno))
    assert await otro.get_client(client_id) is not None
