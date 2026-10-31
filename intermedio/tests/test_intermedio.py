"""Lo que tiene que ser cierto del intermedio (§5 y §7 del diseño).

Easypanel se simula: un panel falso que guarda las llamadas, y un servidor
HTTP local para probar el formato tRPC del cliente.
"""
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from provisioner import app as modulo
from provisioner.app import Config, armar_app
from provisioner.easypanel import Easypanel, ErrorEasypanel

PROD = 'https://gnbsas.odoo.com'
STAGING = 'https://test-gnb.odoo.com'
SECRETO_PROD = 'p' * 40
SECRETO_STAGING = 's' * 40
HUELLA = hashlib.sha256(b'clave').hexdigest()


class PanelFalso:
    def __init__(self):
        self.llamadas = []
        self.servicios = set()
        self.dominios = {}

    def leer(self, procedimiento, **entrada):
        self.llamadas.append(('leer', procedimiento, entrada))
        if procedimiento == 'domains.listDomains':
            return [{'host': h} for h in self.dominios.get(entrada['serviceName'], [])]
        return []

    def escribir(self, procedimiento, **entrada):
        self.llamadas.append(('escribir', procedimiento, entrada))
        if procedimiento == 'services.app.createService':
            self.servicios.add(entrada['serviceName'])
        if procedimiento == 'services.app.destroyService':
            self.servicios.discard(entrada['serviceName'])

    def asegurar_proyecto(self, proyecto):
        self.escribir('projects.createProject', name=proyecto)

    def existe_servicio(self, proyecto, servicio):
        return servicio in self.servicios

    def asegurar_dominio(self, proyecto, servicio, host, puerto):
        # Como el real: si el dominio ya está, no lo crea de nuevo.
        if host in self.dominios.get(servicio, []):
            return
        self.dominios.setdefault(servicio, []).append(host)
        self.escribir('domains.createDomain', host=host, port=puerto)

    def procedimientos(self):
        return [p for _tipo, p, _e in self.llamadas]


@pytest.fixture
def config():
    return Config({
        'EASYPANEL_URL': 'http://panel', 'EASYPANEL_TOKEN': 'token-panel',
        'MCP_IMAGE': 'ghcr.io/wansoft-tech/mcp-cliente:0.2.1',
        'MCP_DOMAIN': 'mcp.wansoft.tech',
        'ORIGENES': '%s c %s\n%s s %s' % (PROD, SECRETO_PROD, STAGING, SECRETO_STAGING),
    })


@pytest.fixture
def panel():
    return PanelFalso()


@pytest.fixture
def cliente(config, panel, monkeypatch):
    monkeypatch.setattr(modulo, '_saludable', lambda host: True)
    return TestClient(armar_app(config, panel))


def firmar(cuerpo, origen=PROD, secreto=SECRETO_PROD, fecha=None):
    fecha = str(int(fecha if fecha is not None else time.time()))
    firma = hmac.new(secreto.encode(), b'%s.%s' % (fecha.encode(), cuerpo), hashlib.sha256).hexdigest()
    return {'X-Wansoft-Origen': origen, 'X-Wansoft-Fecha': fecha, 'X-Wansoft-Firma': firma,
            'Content-Type': 'application/json'}


def alta(conexion_id=12):
    return json.dumps({
        'id': conexion_id,
        'odoo': {'url': 'https://cliente.odoo.com', 'db': 'cliente', 'user': 'persona@cliente.example',
                 'api_key': 'clave-api'},
        'link_secret_sha256': HUELLA,
        'wansoft_token': 'token-wansoft',
    }).encode()


def test_health_sin_firma(cliente):
    assert cliente.get('/health').json() == {'status': 'ok'}


def test_sin_firma_no_hace_nada(cliente, panel):
    respuesta = cliente.post('/connections', content=alta())
    assert respuesta.status_code == 401
    assert panel.llamadas == []


def test_firma_de_otro_secreto(cliente, panel):
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo, secreto='x' * 40))
    assert respuesta.status_code == 401
    assert panel.llamadas == []


def test_firma_vencida(cliente, panel):
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo,
                             headers=firmar(cuerpo, fecha=time.time() - 3600))
    assert respuesta.status_code == 401


def test_cuerpo_alterado(cliente, panel):
    cabeceras = firmar(alta())
    respuesta = cliente.post('/connections', content=alta(99), headers=cabeceras)
    assert respuesta.status_code == 401


def test_alta_completa(cliente, panel):
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json() == {'servicio': 'c-12', 'host': 'c-12.mcp.wansoft.tech', 'listo': True}
    assert panel.procedimientos() == [
        'projects.createProject', 'services.app.createService', 'services.app.updateSourceImage',
        'services.app.updateEnv', 'domains.createDomain', 'services.app.deployService']
    env = [e for t, p, e in panel.llamadas if p == 'services.app.updateEnv'][0]['env']
    assert 'ODOO_API_KEY=clave-api' in env
    assert 'MCP_LINK_SECRET_SHA256=%s' % HUELLA in env
    assert 'WANSOFT_API_URL=%s/wansoft/mcp/v1' % PROD in env
    assert 'ODOO_MCP_ALLOWED_HOSTS=c-12.mcp.wansoft.tech' in env


def test_staging_no_pisa_produccion(cliente, panel):
    """La misma conexión 12 en staging es otro servicio, con su API de Wansoft."""
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo,
                             headers=firmar(cuerpo, origen=STAGING, secreto=SECRETO_STAGING))
    assert respuesta.json()['servicio'] == 's-12'
    env = [e for t, p, e in panel.llamadas if p == 'services.app.updateEnv'][0]['env']
    assert 'WANSOFT_API_URL=%s/wansoft/mcp/v1' % STAGING in env


def test_staging_no_puede_firmar_como_produccion(cliente, panel):
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo,
                             headers=firmar(cuerpo, origen=PROD, secreto=SECRETO_STAGING))
    assert respuesta.status_code == 401


def test_repetir_el_alta_no_duplica(cliente, panel):
    for _vez in range(2):
        cuerpo = alta()
        cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    assert panel.procedimientos().count('services.app.createService') == 1
    assert panel.procedimientos().count('domains.createDomain') == 1


def test_variable_con_salto_de_linea(cliente, panel):
    datos = json.loads(alta())
    datos['odoo']['user'] = 'persona\nODOO_YOLO=true'
    cuerpo = json.dumps(datos).encode()
    respuesta = cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    assert respuesta.status_code == 400
    assert 'services.app.updateEnv' not in panel.procedimientos()


def test_baja(cliente, panel):
    cuerpo = alta()
    cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    respuesta = cliente.delete('/connections/12', headers=firmar(b''))
    assert respuesta.status_code == 200
    assert 'services.app.destroyService' in panel.procedimientos()
    # Repetir la baja no falla.
    assert cliente.delete('/connections/12', headers=firmar(b'')).status_code == 200


def test_estado(cliente, panel):
    respuesta = cliente.get('/connections/12', headers=firmar(b''))
    assert respuesta.json() == {'servicio': 'c-12', 'existe': False, 'listo': False}


# -- Formato tRPC del cliente de Easypanel -------------------------------------
class PanelHttp(BaseHTTPRequestHandler):
    pedidos = []

    def _responder(self, status, datos):
        cuerpo = json.dumps(datos).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        url = urlparse(self.path)
        entrada = json.loads(parse_qs(url.query)['input'][0])
        PanelHttp.pedidos.append(('GET', url.path, entrada, self.headers.get('Authorization')))
        if url.path.endswith('inspectService'):
            return self._responder(404, {'error': {'json': {'message': 'Service not found'}}})
        self._responder(200, {'result': {'data': {'json': [{'name': 'clientes-mcp'}]}}})

    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        PanelHttp.pedidos.append(('POST', self.path, cuerpo, self.headers.get('Authorization')))
        self._responder(200, {'result': {'data': {'json': None}}})

    def log_message(self, *args):
        pass


def test_formato_trpc():
    servidor = HTTPServer(('127.0.0.1', 0), PanelHttp)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 'token-panel')
        panel.asegurar_proyecto('clientes-mcp')
        assert not panel.existe_servicio('clientes-mcp', 'c-1')
        panel.escribir('services.app.deployService', projectName='clientes-mcp', serviceName='c-1')
    finally:
        servidor.shutdown()
    metodo, ruta, entrada, auth = PanelHttp.pedidos[0]
    assert (metodo, ruta) == ('GET', '/api/trpc/projects.listProjects')
    assert entrada == {'json': {}}
    assert auth == 'Bearer token-panel'
    # El proyecto ya existía: no se crea de nuevo.
    assert not [p for p in PanelHttp.pedidos if p[1].endswith('createProject')]
    metodo, ruta, cuerpo, _auth = PanelHttp.pedidos[-1]
    assert (metodo, ruta) == ('POST', '/api/trpc/services.app.deployService')
    assert cuerpo == {'json': {'projectName': 'clientes-mcp', 'serviceName': 'c-1'}}


def test_error_de_easypanel():
    servidor = HTTPServer(('127.0.0.1', 0), PanelHttp)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 't')
        with pytest.raises(ErrorEasypanel) as error:
            panel.leer('services.app.inspectService', projectName='p', serviceName='s')
    finally:
        servidor.shutdown()
    assert 'Service not found' in str(error.value)


def test_dominio_existente_no_se_duplica():
    llamadas = []

    class Fijo(Easypanel):
        def leer(self, procedimiento, **entrada):
            return [{'host': 'c-1.mcp.wansoft.tech'}]

        def escribir(self, procedimiento, **entrada):
            llamadas.append(procedimiento)

    Fijo('http://panel', 't').asegurar_dominio('p', 'c-1', 'c-1.mcp.wansoft.tech', 8000)
    assert llamadas == []
