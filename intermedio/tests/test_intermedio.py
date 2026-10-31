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
        self.envs = {}

    def leer(self, procedimiento, **entrada):
        self.llamadas.append(('leer', procedimiento, entrada))
        if procedimiento == 'domains.listDomains':
            return [{'host': h} for h in self.dominios.get(entrada['serviceName'], [])]
        if procedimiento == 'services.app.inspectService':
            return {'env': self.envs.get(entrada['serviceName'], '')}
        return []

    def escribir(self, procedimiento, **entrada):
        self.llamadas.append(('escribir', procedimiento, entrada))
        if procedimiento == 'services.app.createService':
            self.servicios.add(entrada['serviceName'])
        if procedimiento == 'services.app.destroyService':
            self.servicios.discard(entrada['serviceName'])
        if procedimiento == 'services.app.updateEnv':
            self.envs[entrada['serviceName']] = entrada['env']

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
        consulta = parse_qs(url.query)
        entrada = json.loads(consulta['input'][0]) if 'input' in consulta else None
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
    # La primera lectura va sin entrada: anda igual con o sin superjson.
    assert entrada is None
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


def test_falla_de_easypanel_llega_con_el_motivo(config, monkeypatch):
    """No 5xx: el proxy de Easypanel taparía el motivo con su página de error."""
    class Caido(PanelFalso):
        def asegurar_proyecto(self, proyecto):
            raise ErrorEasypanel('projects.listProjects', 'el panel no respondió', None)

    monkeypatch.setattr(modulo, '_saludable', lambda host: True)
    cliente = TestClient(armar_app(config, Caido()))
    cuerpo = alta()
    respuesta = cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    assert respuesta.status_code == 424
    assert 'projects.listProjects' in respuesta.json()['error']


class PanelPlano(BaseHTTPRequestHandler):
    """Easypanel sin superjson: entrada y respuesta sin el sobre {"json": ...}."""
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
        consulta = parse_qs(url.query)
        entrada = json.loads(consulta['input'][0]) if 'input' in consulta else None
        PanelPlano.pedidos.append(('GET', url.path, entrada))
        if url.path.endswith('listProjects'):
            return self._responder(200, {'result': {'data': [{'name': 'clientes-mcp'}]}})
        self._responder(200, {'result': {'data': {'name': 'c-1'}}})

    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        PanelPlano.pedidos.append(('POST', self.path, cuerpo))
        if 'json' in cuerpo:
            return self._responder(400, {'error': {'message': 'Invalid input', 'code': -32600}})
        self._responder(200, {'result': {'data': None}})

    def log_message(self, *args):
        pass


def test_formato_sin_superjson():
    servidor = HTTPServer(('127.0.0.1', 0), PanelPlano)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 't')
        panel.asegurar_proyecto('clientes-mcp')
        assert panel.existe_servicio('clientes-mcp', 'c-1')
        panel.escribir('services.app.deployService', projectName='clientes-mcp', serviceName='c-1')
    finally:
        servidor.shutdown()
    assert panel.protocolo == 'trpc'
    # El proyecto existente se reconoce y no se intenta crear.
    assert not [p for p in PanelPlano.pedidos if p[1].endswith('createProject')]
    assert ('GET', '/api/trpc/services.app.inspectService',
            {'projectName': 'clientes-mcp', 'serviceName': 'c-1'}) in PanelPlano.pedidos
    assert PanelPlano.pedidos[-1][2] == {'projectName': 'clientes-mcp', 'serviceName': 'c-1'}


def test_error_sin_superjson_trae_el_motivo():
    servidor = HTTPServer(('127.0.0.1', 0), PanelPlano)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 't')
        panel.protocolo = 'trpc-superjson'  # fuerza el formato equivocado
        with pytest.raises(ErrorEasypanel) as error:
            panel.escribir('services.app.deployService', projectName='p', serviceName='s')
    finally:
        servidor.shutdown()
    assert 'Invalid input' in str(error.value)


class PanelOrpc(BaseHTTPRequestHandler):
    """Easypanel con oRPC: {"json": ...} en la raíz, entrada envuelta y POST."""
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
        PanelOrpc.pedidos.append(('GET', url.path, None))
        if 'input' in url.query or 'data' in url.query:
            return self._responder(400, {'json': {'message': 'Input validation failed'}})
        self._responder(200, {'json': [{'name': 'clientes-mcp'}], 'meta': []})

    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        PanelOrpc.pedidos.append(('POST', self.path, cuerpo))
        if 'json' not in cuerpo:
            return self._responder(400, {'json': {'defined': False, 'code': 'BAD_REQUEST', 'status': 400,
                                                  'message': 'Input validation failed'}})
        if self.path.endswith('inspectService'):
            return self._responder(404, {'json': {'message': 'Service not found'}})
        self._responder(200, {'json': None, 'meta': []})

    def log_message(self, *args):
        pass


def test_formato_orpc():
    servidor = HTTPServer(('127.0.0.1', 0), PanelOrpc)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 't')
        panel.asegurar_proyecto('clientes-mcp')
        assert not panel.existe_servicio('clientes-mcp', 'c-1')
        panel.escribir('services.app.createService', projectName='clientes-mcp', serviceName='c-1')
    finally:
        servidor.shutdown()
    assert panel.protocolo == 'orpc'
    assert not [p for p in PanelOrpc.pedidos if p[1].endswith('createProject')]
    # La lectura con entrada va por POST y envuelta.
    assert ('POST', '/api/trpc/services.app.inspectService',
            {'json': {'projectName': 'clientes-mcp', 'serviceName': 'c-1'}}) in PanelOrpc.pedidos
    assert PanelOrpc.pedidos[-1][2] == {'json': {'projectName': 'clientes-mcp', 'serviceName': 'c-1'}}


def test_error_orpc_trae_el_motivo():
    servidor = HTTPServer(('127.0.0.1', 0), PanelOrpc)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    try:
        panel = Easypanel('http://127.0.0.1:%d' % servidor.server_port, 't')
        panel.protocolo = 'trpc'  # fuerza el formato equivocado
        with pytest.raises(ErrorEasypanel) as error:
            panel.escribir('projects.createProject', name='x')
    finally:
        servidor.shutdown()
    assert 'Input validation failed' in str(error.value)


# -- Niveles y equipo -----------------------------------------------------------
def _env_de(panel, servicio):
    return dict(l.split('=', 1) for l in panel.envs[servicio].splitlines())


def test_alta_arranca_en_lectura(cliente, panel):
    cuerpo = alta()
    cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    env = _env_de(panel, 'c-12')
    assert env['MCP_PERFIL'] == 'cliente'
    assert env['MCP_NIVEL'] == 'lectura'


def test_alta_con_nivel(cliente, panel):
    datos = json.loads(alta())
    datos['nivel'] = 'escritura'
    cuerpo = json.dumps(datos).encode()
    cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    assert _env_de(panel, 'c-12')['MCP_NIVEL'] == 'escritura'


def test_nivel_invalido(cliente, panel):
    datos = json.loads(alta())
    datos['nivel'] = 'todo'
    cuerpo = json.dumps(datos).encode()
    assert cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo)).status_code == 400


def test_cambiar_nivel_conserva_el_resto(cliente, panel):
    cuerpo = alta()
    cliente.post('/connections', content=cuerpo, headers=firmar(cuerpo))
    antes = _env_de(panel, 'c-12')
    pedido = json.dumps({'nivel': 'metodos'}).encode()
    respuesta = cliente.post('/connections/12/nivel', content=pedido, headers=firmar(pedido))
    assert respuesta.status_code == 200, respuesta.text
    despues = _env_de(panel, 'c-12')
    assert despues['MCP_NIVEL'] == 'metodos'
    assert {k: v for k, v in despues.items() if k != 'MCP_NIVEL'} == \
        {k: v for k, v in antes.items() if k != 'MCP_NIVEL'}
    assert panel.procedimientos()[-1] == 'services.app.deployService'


def test_cambiar_nivel_sin_firma(cliente, panel):
    pedido = json.dumps({'nivel': 'metodos'}).encode()
    assert cliente.post('/connections/12/nivel', content=pedido).status_code == 401


def test_cambiar_nivel_de_un_servicio_que_no_es_nuestro(cliente, panel):
    pedido = json.dumps({'nivel': 'metodos'}).encode()
    respuesta = cliente.post('/connections/99/nivel', content=pedido, headers=firmar(pedido))
    assert respuesta.status_code == 400
    assert 'services.app.updateEnv' not in panel.procedimientos()


def test_equipo(cliente, panel):
    cuerpo = json.dumps({
        'id': 3,
        'odoo': {'url': 'https://cliente.odoo.com', 'db': 'cliente-staging', 'user': 'mcp@wansoft.tech',
                 'api_key': 'clave-equipo'},
        'link_secret_sha256': HUELLA,
    }).encode()
    respuesta = cliente.post('/equipo', content=cuerpo, headers=firmar(cuerpo))
    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()['servicio'] == 'ce-3'
    env = _env_de(panel, 'ce-3')
    assert env['MCP_PERFIL'] == 'equipo'
    assert 'WANSOFT_TOKEN' not in env and 'MCP_NIVEL' not in env
    # El 3 del equipo no es la conexión 3 de un cliente.
    assert cliente.delete('/connections/3', headers=firmar(b'')).json()['servicio'] == 'c-3'
    assert cliente.delete('/equipo/3', headers=firmar(b'')).json()['servicio'] == 'ce-3'
    assert 'ce-3' not in panel.servicios
