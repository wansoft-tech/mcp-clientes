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
# Cuando falla Easypanel se responde 424 y no 502: el proxy de Easypanel
# reemplaza las respuestas 5xx de un servicio por su propia página de error, y
# el motivo no llegaría nunca a Odoo.
FALLA_EASYPANEL = 424
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


NIVELES = ('lectura', 'escritura', 'metodos')


def _nombre(config, origen, conexion_id, perfil='cliente'):
    """c-12 para una persona del cliente; ce-3 para la base 3 del equipo."""
    prefijo, _secreto = config.origenes[origen]
    return '%s%s-%d' % (prefijo, 'e' if perfil == 'equipo' else '', conexion_id)


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


def crear_o_actualizar(config, panel, origen, datos, perfil='cliente'):
    conexion_id = int(datos['id'])
    odoo = datos['odoo']
    servicio = _nombre(config, origen, conexion_id, perfil)
    host = '%s.%s' % (servicio, config.dominio)
    variables = {
        'MCP_PERFIL': perfil,
        'ODOO_URL': odoo['url'],
        'ODOO_DB': odoo['db'],
        'ODOO_USER': odoo['user'],
        'ODOO_API_KEY': odoo['api_key'],
        'ODOO_ALLOWED_COMPANIES': odoo.get('allowed_companies') or None,
        'ODOO_MCP_ALLOWED_HOSTS': host,
        'MCP_LINK_SECRET_SHA256': datos['link_secret_sha256'],
    }
    if perfil == 'cliente':
        nivel = datos.get('nivel') or 'lectura'
        if nivel not in NIVELES:
            raise ValueError('nivel tiene que ser lectura, escritura o metodos.')
        variables.update({
            'MCP_NIVEL': nivel,
            'WANSOFT_API_URL': '%s/wansoft/mcp/v1' % origen,
            'WANSOFT_TOKEN': datos['wansoft_token'],
        })
    env = _env(variables)
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


def cambiar_nivel(config, panel, origen, conexion_id, nivel):
    """Cambia el nivel de una persona sin pedirle de nuevo su clave de API.

    Lee las variables del servicio, reemplaza solo MCP_NIVEL y vuelve a
    desplegar. updateEnv reemplaza todas: por eso se manda el resto tal cual.
    """
    if nivel not in NIVELES:
        raise ValueError('nivel tiene que ser lectura, escritura o metodos.')
    servicio = _nombre(config, origen, conexion_id)
    datos = panel.leer('services.app.inspectService', projectName=config.proyecto,
                       serviceName=servicio) or {}
    lineas = [l for l in (datos.get('env') or '').splitlines()
              if l.strip() and not l.startswith('MCP_NIVEL=')]
    if not any(l.startswith('MCP_LINK_SECRET_SHA256=') for l in lineas):
        raise ValueError('El servicio %s no tiene la configuración esperada.' % servicio)
    lineas.append('MCP_NIVEL=%s' % nivel)
    panel.escribir('services.app.updateEnv', projectName=config.proyecto,
                   serviceName=servicio, env='\n'.join(lineas))
    panel.escribir('services.app.deployService', projectName=config.proyecto, serviceName=servicio)
    return {'servicio': servicio, 'nivel': nivel}


def eliminar(config, panel, origen, conexion_id, perfil='cliente'):
    servicio = _nombre(config, origen, conexion_id, perfil)
    if panel.existe_servicio(config.proyecto, servicio):
        # Los dominios del servicio se van con él.
        panel.escribir('services.app.destroyService', projectName=config.proyecto, serviceName=servicio)
    return {'servicio': servicio, 'eliminado': True}


def armar_app(config=None, panel=None):
    config = config or Config()
    panel = panel or Easypanel(config.easypanel_url, config.easypanel_token)

    async def salud(request):
        return JSONResponse({'status': 'ok'})

    def _falla(error):
        _logger.warning('Falló Easypanel en %s: %s', error.procedimiento, error)
        return _error('Easypanel rechazó %s' % error, FALLA_EASYPANEL)

    def alta(perfil):
        async def ruta(request):
            origen, cuerpo, error = await _verificar(request, config)
            if error:
                return error
            try:
                datos = json.loads(cuerpo)
                resultado = await run_in_threadpool(crear_o_actualizar, config, panel, origen, datos, perfil)
            except (ValueError, KeyError, TypeError) as error:
                return _error('Pedido inválido: %s' % error, 400)
            except ErrorEasypanel as error:
                return _falla(error)
            return JSONResponse(resultado)
        return ruta

    def una(perfil):
        async def ruta(request):
            origen, _cuerpo, error = await _verificar(request, config)
            if error:
                return error
            try:
                conexion_id = int(request.path_params['conexion_id'])
                if request.method == 'DELETE':
                    resultado = await run_in_threadpool(eliminar, config, panel, origen, conexion_id, perfil)
                else:
                    servicio = _nombre(config, origen, conexion_id, perfil)
                    existe = await run_in_threadpool(panel.existe_servicio, config.proyecto, servicio)
                    host = '%s.%s' % (servicio, config.dominio)
                    resultado = {'servicio': servicio, 'existe': existe,
                                 'listo': existe and await run_in_threadpool(_saludable, host)}
            except ErrorEasypanel as error:
                return _falla(error)
            return JSONResponse(resultado)
        return ruta

    async def nivel(request):
        origen, cuerpo, error = await _verificar(request, config)
        if error:
            return error
        try:
            datos = json.loads(cuerpo)
            resultado = await run_in_threadpool(
                cambiar_nivel, config, panel, origen, int(request.path_params['conexion_id']), datos['nivel'])
        except (ValueError, KeyError, TypeError) as error:
            return _error('Pedido inválido: %s' % error, 400)
        except ErrorEasypanel as error:
            return _falla(error)
        return JSONResponse(resultado)

    return Starlette(routes=[
        Route('/health', salud, methods=['GET']),
        Route('/connections', alta('cliente'), methods=['POST']),
        Route('/connections/{conexion_id:int}', una('cliente'), methods=['GET', 'DELETE']),
        Route('/connections/{conexion_id:int}/nivel', nivel, methods=['POST']),
        Route('/equipo', alta('equipo'), methods=['POST']),
        Route('/equipo/{conexion_id:int}', una('equipo'), methods=['GET', 'DELETE']),
    ])
