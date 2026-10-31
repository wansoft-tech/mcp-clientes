"""Intermedio «mcp-provisioner»: levanta y elimina los servidores MCP de cada persona.

Lo llama el Odoo de Wansoft (módulo wansoft_claude_connect) y solo responde a
pedidos firmados. Es el único que tiene el token de Easypanel.

Firma: cabeceras X-Wansoft-Origen (la dirección del Odoo que llama),
X-Wansoft-Fecha (segundos Unix) y X-Wansoft-Firma, el HMAC-SHA256 en
hexadecimal de "<fecha>.<cuerpo>" con el secreto de ese origen. Cada origen
—producción, staging— tiene su propio secreto y su propio prefijo de
servicio: una copia de producción en staging no puede tocar los servidores de
producción, porque no tiene su secreto.

Nunca se registran cuerpos ni variables: llevan la clave de API del cliente.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import time
import urllib.request

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Route

from .easypanel import Easypanel, ErrorEasypanel

_logger = logging.getLogger('provisioner')

VENTANA_FIRMA = 300
PUERTO_SERVIDOR = 8000
ESPERA_SALUD = 20


class Config:
    def __init__(self, entorno=os.environ):
        self.easypanel_url = entorno['EASYPANEL_URL']
        self.easypanel_token = entorno['EASYPANEL_TOKEN']
        self.imagen = entorno['MCP_IMAGE']
        self.dominio = entorno['MCP_DOMAIN']
        self.proyecto = entorno.get('MCP_PROJECT', 'clientes-mcp')
        self.origenes = self._origenes(entorno['ORIGENES'])

    @staticmethod
    def _origenes(texto):
        """ORIGENES: una línea por Odoo que puede llamar: <url> <prefijo> <secreto>.

        Por ejemplo «https://gnbsas.odoo.com c <secreto>». El prefijo da el
        nombre de los servicios (c-12) y la API de Wansoft es <url>/wansoft/mcp/v1.
        """
        origenes = {}
        for linea in texto.replace(';', '\n').splitlines():
            if not linea.strip():
                continue
            url, prefijo, secreto = linea.split()
            if not re.fullmatch(r'[a-z]{1,3}', prefijo) or len(secreto) < 32:
                raise ValueError('ORIGENES: prefijo de 1 a 3 letras y secreto de 32 caracteres o más.')
            origenes[url.rstrip('/')] = (prefijo, secreto)
        if not origenes:
            raise ValueError('ORIGENES está vacío.')
        return origenes


def _error(mensaje, status):
    return JSONResponse({'error': mensaje}, status_code=status)


async def _verificar(request, config):
    """El origen y el cuerpo, o una respuesta de error si la firma no vale."""
    origen = (request.headers.get('x-wansoft-origen') or '').rstrip('/')
    fecha = request.headers.get('x-wansoft-fecha') or ''
    firma = request.headers.get('x-wansoft-firma') or ''
    cuerpo = await request.body()
    if origen not in config.origenes or not fecha.isdigit():
        return None, None, _error('Firma inválida.', 401)
    if abs(time.time() - int(fecha)) > VENTANA_FIRMA:
        return None, None, _error('Firma vencida.', 401)
    _prefijo, secreto = config.origenes[origen]
    esperada = hmac.new(secreto.encode(), b'%s.%s' % (fecha.encode(), cuerpo), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(esperada, firma):
        return None, None, _error('Firma inválida.', 401)
    return origen, cuerpo, None


def _nombre(config, origen, conexion_id):
    prefijo, _secreto = config.origenes[origen]
    return '%s-%d' % (prefijo, conexion_id)


def _env(valores):
    lineas = []
    for clave, valor in valores.items():
        valor = '' if valor is None else str(valor)
        if '\n' in valor or '\r' in valor:
            raise ValueError('La variable %s tiene un salto de línea.' % clave)
        lineas.append('%s=%s' % (clave, valor))
    return '\n'.join(lineas)


def _saludable(host):
    try:
        with urllib.request.urlopen('https://%s/health' % host, timeout=5) as respuesta:
            return respuesta.status == 200
    except Exception:
        return False


def crear_o_actualizar(config, panel, origen, datos):
    conexion_id = int(datos['id'])
    odoo = datos['odoo']
    servicio = _nombre(config, origen, conexion_id)
    host = '%s.%s' % (servicio, config.dominio)
    env = _env({
        'ODOO_URL': odoo['url'],
        'ODOO_DB': odoo['db'],
        'ODOO_USER': odoo['user'],
        'ODOO_API_KEY': odoo['api_key'],
        'ODOO_ALLOWED_COMPANIES': odoo.get('allowed_companies') or None,
        'ODOO_MCP_ALLOWED_HOSTS': host,
        'MCP_LINK_SECRET_SHA256': datos['link_secret_sha256'],
        'WANSOFT_API_URL': '%s/wansoft/mcp/v1' % origen,
        'WANSOFT_TOKEN': datos['wansoft_token'],
    })
    if not re.fullmatch(r'[0-9a-f]{64}', datos['link_secret_sha256']):
        raise ValueError('link_secret_sha256 tiene que ser una huella SHA-256.')

    panel.asegurar_proyecto(config.proyecto)
    if not panel.existe_servicio(config.proyecto, servicio):
        panel.escribir('services.app.createService', projectName=config.proyecto, serviceName=servicio)
    panel.escribir('services.app.updateSourceImage', projectName=config.proyecto,
                   serviceName=servicio, image=config.imagen)
    panel.escribir('services.app.updateEnv', projectName=config.proyecto,
                   serviceName=servicio, env=env)
    panel.asegurar_dominio(config.proyecto, servicio, host, PUERTO_SERVIDOR)
    panel.escribir('services.app.deployService', projectName=config.proyecto, serviceName=servicio)

    limite = time.time() + ESPERA_SALUD
    listo = _saludable(host)
    while not listo and time.time() < limite:
        time.sleep(2)
        listo = _saludable(host)
    return {'servicio': servicio, 'host': host, 'listo': listo}


def eliminar(config, panel, origen, conexion_id):
    servicio = _nombre(config, origen, conexion_id)
    if panel.existe_servicio(config.proyecto, servicio):
        # Los dominios del servicio se van con él.
        panel.escribir('services.app.destroyService', projectName=config.proyecto, serviceName=servicio)
    return {'servicio': servicio, 'eliminado': True}


def armar_app(config=None, panel=None):
    config = config or Config()
    panel = panel or Easypanel(config.easypanel_url, config.easypanel_token)

    async def salud(request):
        return JSONResponse({'status': 'ok'})

    async def conexiones(request):
        origen, cuerpo, error = await _verificar(request, config)
        if error:
            return error
        try:
            datos = json.loads(cuerpo)
            resultado = await run_in_threadpool(crear_o_actualizar, config, panel, origen, datos)
        except (ValueError, KeyError, TypeError) as error:
            return _error('Pedido inválido: %s' % error, 400)
        except ErrorEasypanel as error:
            _logger.warning('Falló Easypanel en %s', error.procedimiento)
            return _error('Easypanel rechazó %s: %s' % (error.procedimiento, error), 502)
        return JSONResponse(resultado)

    async def conexion(request):
        origen, _cuerpo, error = await _verificar(request, config)
        if error:
            return error
        try:
            conexion_id = int(request.path_params['conexion_id'])
            if request.method == 'DELETE':
                resultado = await run_in_threadpool(eliminar, config, panel, origen, conexion_id)
            else:
                servicio = _nombre(config, origen, conexion_id)
                existe = await run_in_threadpool(panel.existe_servicio, config.proyecto, servicio)
                host = '%s.%s' % (servicio, config.dominio)
                resultado = {'servicio': servicio, 'existe': existe,
                             'listo': existe and await run_in_threadpool(_saludable, host)}
        except ErrorEasypanel as error:
            _logger.warning('Falló Easypanel en %s', error.procedimiento)
            return _error('Easypanel rechazó %s: %s' % (error.procedimiento, error), 502)
        return JSONResponse(resultado)

    return Starlette(routes=[
        Route('/health', salud, methods=['GET']),
        Route('/connections', conexiones, methods=['POST']),
        Route('/connections/{conexion_id:int}', conexion, methods=['GET', 'DELETE']),
    ])
