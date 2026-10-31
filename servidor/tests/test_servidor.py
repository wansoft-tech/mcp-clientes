"""Lo que tiene que ser cierto del servidor del cliente antes del piloto (§7 del diseño).

Corre sin Odoo: el original conecta recién en la primera llamada a su base.
Wansoft se simula con un servidor HTTP local.
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx2 as httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

CLAVE = 'clave-de-prueba-del-enlace'
TOKEN = 'token-de-prueba'
LLAMADAS = []


class WansoftFalso(BaseHTTPRequestHandler):
    def do_POST(self):
        cuerpo = json.loads(self.rfile.read(int(self.headers['Content-Length'])) or b'{}')
        LLAMADAS.append((self.path, self.headers.get('Authorization'), cuerpo))
        if self.path.endswith('/confirmar_consulta'):
            respuesta, status = {'error': 'Ese identificador no existe.'}, 400
        else:
            respuesta, status = {'proyectos': [{'id': 1, 'nombre': 'Implementación'}]}, 200
        datos = json.dumps(respuesta).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def wansoft():
    servidor = HTTPServer(('127.0.0.1', 0), WansoftFalso)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    yield 'http://127.0.0.1:%d/wansoft/mcp/v1' % servidor.server_port
    servidor.shutdown()


@pytest.fixture
def entorno(monkeypatch, wansoft):
    for clave, valor in {
        'ODOO_URL': 'https://cliente.example', 'ODOO_DB': 'cliente',
        'ODOO_USER': 'persona@cliente.example', 'ODOO_API_KEY': 'clave-api',
        'ODOO_YOLO': 'true', 'ODOO_MCP_ENABLE_METHOD_CALLS': 'true',
        'MCP_LINK_SECRET_SHA256': hashlib.sha256(CLAVE.encode()).hexdigest(),
        'WANSOFT_API_URL': wansoft, 'WANSOFT_TOKEN': TOKEN,
    }.items():
        monkeypatch.setenv(clave, valor)


@pytest.fixture
def servidor(entorno):
    from wansoft_mcp_cliente.app import armar_servidor
    return armar_servidor()


@pytest.fixture
def app(servidor):
    from wansoft_mcp_cliente.app import armar_app
    return armar_app(servidor)


def nombres(servidor):
    return {h.name for h in servidor.app._tool_manager.list_tools()}


def test_solo_lectura_aunque_pidan_escritura(servidor):
    """Con ODOO_YOLO=true y method calls pedidos, igual no hay herramientas de escritura."""
    assert servidor.config.yolo_mode == 'read'
    for prohibida in ('create_record', 'update_record', 'delete_record', 'post_message',
                      'upload_attachment', 'call_model_method', 'create_records', 'update_records'):
        assert prohibida not in nombres(servidor)
    assert 'search_records' in nombres(servidor)


def test_herramientas_de_wansoft(servidor):
    assert {'wansoft_mis_proyectos', 'wansoft_mis_tareas', 'wansoft_ver_tarea', 'wansoft_mi_equipo',
            'wansoft_preparar_consulta', 'wansoft_confirmar_consulta'} <= nombres(servidor)


def test_instrucciones_en_espanol(servidor):
    instrucciones = servidor.app.instructions
    assert 'wansoft_preparar_consulta' in instrucciones
    assert 'esperá su confirmación' in instrucciones


async def _pedir(app, metodo, ruta, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as cliente:
        return await cliente.request(metodo, ruta, **kwargs)


async def test_sin_clave_404(app):
    for ruta in ('/mcp', '/otra-clave/mcp', '/', '/%s' % CLAVE, '/%s/otra' % CLAVE):
        respuesta = await _pedir(app, 'POST', ruta, json={})
        assert respuesta.status_code == 404, ruta


async def test_health_sin_datos(app):
    respuesta = await _pedir(app, 'GET', '/health')
    assert respuesta.status_code == 200
    assert respuesta.json() == {'status': 'ok'}


def test_clave_mal_configurada(entorno, servidor, monkeypatch):
    from wansoft_mcp_cliente.app import armar_app
    monkeypatch.setenv('MCP_LINK_SECRET_SHA256', CLAVE)
    with pytest.raises(ValueError):
        armar_app(servidor)


@pytest.fixture
def puerto_libre():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


@pytest.fixture
def corriendo(app, puerto_libre):
    """El servidor de verdad, con uvicorn, para hablarle con un cliente MCP."""
    import time

    import uvicorn
    config = uvicorn.Config(app, host='127.0.0.1', port=puerto_libre, log_level='warning')
    servidor = uvicorn.Server(config)
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()
    while not servidor.started:
        time.sleep(0.05)
    yield 'http://127.0.0.1:%d' % puerto_libre
    servidor.should_exit = True
    hilo.join(5)


async def test_cliente_mcp_con_la_clave(corriendo):
    LLAMADAS.clear()
    async with streamable_http_client('%s/%s/mcp' % (corriendo, CLAVE)) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            inicio = await sesion.initialize()
            assert 'Wansoft' in (inicio.instructions or '')
            herramientas = {h.name for h in (await sesion.list_tools()).tools}
            assert 'wansoft_mis_proyectos' in herramientas
            assert 'delete_record' not in herramientas

            resultado = await sesion.call_tool('wansoft_mis_proyectos', {})
            assert not resultado.is_error
            assert LLAMADAS[-1][0].endswith('/mis_proyectos')
            assert LLAMADAS[-1][1] == 'Bearer %s' % TOKEN

            # El error de Wansoft le llega a Claude con el motivo.
            resultado = await sesion.call_tool('wansoft_confirmar_consulta', {'identificador': 'x'})
            assert resultado.is_error
            assert 'Ese identificador no existe.' in resultado.content[0].text


async def test_wansoft_responde_aunque_su_base_no(corriendo):
    """Su Odoo no existe en estos tests: la búsqueda en su base falla y Wansoft igual responde."""
    async with streamable_http_client('%s/%s/mcp' % (corriendo, CLAVE)) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            su_base = await sesion.call_tool('search_records', {'model': 'res.partner'})
            assert su_base.is_error
            assert 'cliente.example' not in su_base.content[0].text
            assert not (await sesion.call_tool('wansoft_mi_equipo', {})).is_error


# -- Perfiles y niveles ------------------------------------------------------
def _armar(monkeypatch, **variables):
    for clave, valor in variables.items():
        monkeypatch.setenv(clave, valor)
    from wansoft_mcp_cliente.app import armar_servidor
    return armar_servidor()


def test_nivel_por_defecto_es_lectura(entorno, monkeypatch):
    monkeypatch.delenv('MCP_NIVEL', raising=False)
    servidor = _armar(monkeypatch)
    assert 'create_record' not in nombres(servidor)
    assert 'SOLO LECTURA' in servidor.app.instructions


def test_nivel_escritura(entorno, monkeypatch):
    servidor = _armar(monkeypatch, MCP_NIVEL='escritura')
    herramientas = nombres(servidor)
    assert {'create_record', 'update_record', 'wansoft_mis_tareas'} <= herramientas
    assert 'delete_record' not in herramientas
    assert 'call_model_method' not in herramientas


def test_nivel_metodos(entorno, monkeypatch):
    servidor = _armar(monkeypatch, MCP_NIVEL='metodos')
    herramientas = nombres(servidor)
    assert {'create_record', 'call_model_method'} <= herramientas
    assert 'delete_record' not in herramientas


def test_perfil_equipo(entorno, monkeypatch):
    monkeypatch.delenv('WANSOFT_API_URL')
    monkeypatch.delenv('WANSOFT_TOKEN')
    servidor = _armar(monkeypatch, MCP_PERFIL='equipo', MCP_NIVEL='lectura')
    herramientas = nombres(servidor)
    assert {'delete_record', 'call_model_method', 'create_record'} <= herramientas
    assert not [h for h in herramientas if h.startswith('wansoft_')]
    assert 'equipo de Wansoft' in servidor.app.instructions


def test_valor_desconocido_no_abre_nada(entorno, monkeypatch):
    import pytest as _pytest
    with _pytest.raises(ValueError):
        _armar(monkeypatch, MCP_NIVEL='todo')
    with _pytest.raises(ValueError):
        _armar(monkeypatch, MCP_PERFIL='admin')


# -- Base detectada ------------------------------------------------------------
class OdooConUnaBase(BaseHTTPRequestHandler):
    bases = ['cliente-staging-123']

    def do_POST(self):
        datos = json.dumps({'jsonrpc': '2.0', 'id': None, 'result': self.bases}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *args):
        pass


def _odoo_falso(bases):
    OdooConUnaBase.bases = bases
    servidor = HTTPServer(('127.0.0.1', 0), OdooConUnaBase)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def test_detecta_la_base():
    from wansoft_mcp_cliente.app import detectar_base
    odoo = _odoo_falso(['cliente-staging-123'])
    try:
        entorno = {'ODOO_URL': 'http://127.0.0.1:%d' % odoo.server_port, 'ODOO_DB': ''}
        detectar_base(entorno)
        assert entorno['ODOO_DB'] == 'cliente-staging-123'
    finally:
        odoo.shutdown()


def test_no_toca_una_base_cargada():
    from wansoft_mcp_cliente.app import detectar_base
    entorno = {'ODOO_URL': 'http://no-se-llama', 'ODOO_DB': 'fija'}
    detectar_base(entorno)
    assert entorno['ODOO_DB'] == 'fija'


def test_varias_bases_no_adivina():
    from wansoft_mcp_cliente.app import detectar_base
    odoo = _odoo_falso(['una', 'otra'])
    try:
        with pytest.raises(ValueError):
            detectar_base({'ODOO_URL': 'http://127.0.0.1:%d' % odoo.server_port, 'ODOO_DB': 'auto'})
    finally:
        odoo.shutdown()


def test_odoo_online_usa_el_subdominio(monkeypatch):
    from wansoft_mcp_cliente import app as modulo
    def sin_lista(*a, **k):
        raise OSError('lista cerrada')
    monkeypatch.setattr(modulo.urllib.request, 'urlopen', sin_lista)
    entorno = {'ODOO_URL': 'https://assist-prueba2.odoo.com', 'ODOO_DB': ''}
    modulo.detectar_base(entorno)
    assert entorno['ODOO_DB'] == 'assist-prueba2'


def test_sin_lista_y_sin_odoo_online_no_adivina(monkeypatch):
    from wansoft_mcp_cliente import app as modulo
    def sin_lista(*a, **k):
        raise OSError('lista cerrada')
    monkeypatch.setattr(modulo.urllib.request, 'urlopen', sin_lista)
    with pytest.raises(ValueError):
        modulo.detectar_base({'ODOO_URL': 'https://erp.cliente.com', 'ODOO_DB': ''})
