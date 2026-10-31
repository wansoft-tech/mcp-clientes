"""Cliente mínimo de la API de Easypanel (tRPC sobre HTTP).

Lectura: GET /api/trpc/<procedimiento>?input=<json>. Escritura: POST con el
cuerpo JSON. En los dos casos, Authorization: Bearer <token>.

Hay dos formatos según la versión de Easypanel: con superjson, la entrada y la
respuesta van envueltas en {"json": ...}; sin él, van tal cual. El formato se
detecta en la primera respuesta —una lectura sin entrada, que anda igual en los
dos— y se usa ese para todo lo demás.

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
        self.superjson = None  # se detecta en la primera respuesta

    def _envolver(self, entrada):
        return {'json': entrada} if self.superjson else entrada

    def _detectar(self, datos):
        if self.superjson is None and datos is not None:
            self.superjson = isinstance(datos, dict) and set(datos) <= {'json', 'meta'} and 'json' in datos
        if self.superjson and isinstance(datos, dict):
            return datos.get('json')
        return datos

    def _pedir(self, procedimiento, entrada, escritura):
        if self.superjson is None and entrada:
            # Hace falta saber el formato antes de mandar una entrada.
            self.leer('projects.listProjects')
        url = '%s/api/trpc/%s' % (self.url, procedimiento)
        cabeceras = {'Authorization': 'Bearer %s' % self.token}
        if escritura:
            datos = json.dumps(self._envolver(entrada)).encode()
            cabeceras['Content-Type'] = 'application/json'
            pedido = urllib.request.Request(url, data=datos, method='POST', headers=cabeceras)
        else:
            if entrada:
                url += '?input=' + urllib.parse.quote(json.dumps(self._envolver(entrada)))
            pedido = urllib.request.Request(url, method='GET', headers=cabeceras)
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT) as respuesta:
                cuerpo = json.loads(respuesta.read() or b'{}')
        except urllib.error.HTTPError as error:
            crudo = error.read()
            try:
                falla = json.loads(crudo)['error']
                falla = falla.get('json', falla)
                detalle = 'HTTP %s: %s' % (error.code, falla.get('message', ''))
            except (ValueError, KeyError, TypeError, AttributeError):
                detalle = 'HTTP %s: %s' % (error.code, crudo[:200].decode(errors='replace'))
            raise ErrorEasypanel(procedimiento, detalle[:400], error.code) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise ErrorEasypanel(procedimiento, 'el panel no respondió (%s)' % error) from None
        return self._detectar((cuerpo.get('result') or {}).get('data'))

    def leer(self, procedimiento, **entrada):
        return self._pedir(procedimiento, entrada, escritura=False)

    def escribir(self, procedimiento, **entrada):
        return self._pedir(procedimiento, entrada, escritura=True)

    # ------------------------------------------------------------------
    def asegurar_proyecto(self, proyecto):
        if proyecto in self._proyectos():
            return
        try:
            self.escribir('projects.createProject', name=proyecto)
        except ErrorEasypanel:
            # Si ya existía —otro alta lo creó recién, o la lista no lo
            # mostró—, se sigue; si no, la falla es real.
            if proyecto not in self._proyectos():
                raise

    def _proyectos(self):
        proyectos = self.leer('projects.listProjects') or []
        return {p.get('name') if isinstance(p, dict) else p for p in proyectos}

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
