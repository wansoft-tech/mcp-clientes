"""Cliente mínimo de la API de Easypanel (tRPC sobre HTTP).

Lectura: GET /api/trpc/<procedimiento>?input=<json>. Escritura: POST con el
cuerpo {"json": ...}. En los dos casos, Authorization: Bearer <token>.

Ojo: las mutaciones update* reemplazan todo lo que no se manda. Acá se manda
siempre el valor completo: la imagen entera y todas las variables.
"""
import json
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 30


class ErrorEasypanel(Exception):
    def __init__(self, procedimiento, mensaje, status=None):
        super().__init__('%s: %s' % (procedimiento, mensaje))
        self.procedimiento = procedimiento
        self.status = status


def nuevo_id():
    """Easypanel pide un id con forma de CUID al crear un dominio, aunque lo regenera."""
    return 'c' + ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(24))


class Easypanel:
    def __init__(self, url, token):
        self.url = url.rstrip('/')
        self.token = token

    def _pedir(self, procedimiento, entrada, escritura):
        url = '%s/api/trpc/%s' % (self.url, procedimiento)
        cabeceras = {'Authorization': 'Bearer %s' % self.token}
        if escritura:
            datos = json.dumps({'json': entrada}).encode()
            cabeceras['Content-Type'] = 'application/json'
            pedido = urllib.request.Request(url, data=datos, method='POST', headers=cabeceras)
        else:
            url += '?input=' + urllib.parse.quote(json.dumps({'json': entrada}))
            pedido = urllib.request.Request(url, method='GET', headers=cabeceras)
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT) as respuesta:
                cuerpo = json.loads(respuesta.read() or b'{}')
        except urllib.error.HTTPError as error:
            try:
                detalle = json.loads(error.read())['error']['json']['message']
            except (ValueError, KeyError, TypeError):
                detalle = 'HTTP %s' % error.code
            raise ErrorEasypanel(procedimiento, detalle, error.code) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise ErrorEasypanel(procedimiento, 'el panel no respondió (%s)' % error) from None
        return ((cuerpo.get('result') or {}).get('data') or {}).get('json')

    def leer(self, procedimiento, **entrada):
        return self._pedir(procedimiento, entrada, escritura=False)

    def escribir(self, procedimiento, **entrada):
        return self._pedir(procedimiento, entrada, escritura=True)

    # ------------------------------------------------------------------
    def asegurar_proyecto(self, proyecto):
        nombres = {p.get('name') for p in (self.leer('projects.listProjects') or [])}
        if proyecto not in nombres:
            self.escribir('projects.createProject', name=proyecto)

    def existe_servicio(self, proyecto, servicio):
        try:
            self.leer('services.app.inspectService', projectName=proyecto, serviceName=servicio)
            return True
        except ErrorEasypanel as error:
            if error.status in (400, 404):
                return False
            raise

    def asegurar_dominio(self, proyecto, servicio, host, puerto):
        dominios = self.leer('domains.listDomains', projectName=proyecto, serviceName=servicio) or []
        if any(d.get('host') == host for d in dominios):
            return
        self.escribir(
            'domains.createDomain',
            id=nuevo_id(), https=True, host=host, path='/', middlewares=[],
            certificateResolver='', wildcard=False, destinationType='service',
            serviceDestination={
                'protocol': 'http', 'port': puerto, 'path': '/',
                'projectName': proyecto, 'serviceName': servicio, 'composeService': '',
            })
