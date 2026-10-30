"""Las herramientas de Wansoft: llaman a la API del módulo wansoft_claude_connect.

POST {WANSOFT_API_URL}/<herramienta> con un cuerpo JSON y el token de la
conexión. La API responde JSON; si falla, {"error": "..."} con el motivo en
palabras del usuario, que se le pasa a Claude tal cual.
"""
import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

TIMEOUT = 30

LECTURA = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                          idempotent_hint=True, open_world_hint=False)
PREPARA = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                          idempotent_hint=False, open_world_hint=False)
CREA = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                       idempotent_hint=False, open_world_hint=True)


class ApiWansoft:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip('/')
        self.token = token

    def _post(self, herramienta: str, parametros: dict) -> Any:
        cuerpo = json.dumps({k: v for k, v in parametros.items() if v is not None}).encode()
        pedido = urllib.request.Request(
            '%s/%s' % (self.url, herramienta), data=cuerpo, method='POST',
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer %s' % self.token})
        try:
            with urllib.request.urlopen(pedido, timeout=TIMEOUT) as respuesta:
                return json.loads(respuesta.read())
        except urllib.error.HTTPError as error:
            try:
                mensaje = json.loads(error.read()).get('error')
            except ValueError:
                mensaje = None
            raise ToolError(mensaje or 'Wansoft respondió con un error (%s).' % error.code) from None
        except (urllib.error.URLError, TimeoutError):
            raise ToolError('No pude comunicarme con Wansoft. Probá de nuevo en unos minutos.') from None

    async def llamar(self, herramienta: str, **parametros) -> Any:
        return await asyncio.to_thread(self._post, herramienta, parametros)


def registrar(app, api: ApiWansoft) -> None:
    """Registra las seis herramientas de Wansoft en el servidor MCP."""

    @app.tool(name='wansoft_mis_proyectos', title='Mis proyectos con Wansoft', annotations=LECTURA)
    async def wansoft_mis_proyectos() -> dict:
        """Los proyectos de la persona con Wansoft y sus alcances (objetivos): responsable, fecha de compromiso, si está en fecha o vencido, y cuántos entregables están hechos sobre el total."""
        return await api.llamar('mis_proyectos')

    @app.tool(name='wansoft_mis_tareas', title='Mis tareas en Wansoft', annotations=LECTURA)
    async def wansoft_mis_tareas(estado: str = 'abiertas', proyecto_id: Optional[int] = None,
                                 limite: int = 50) -> dict:
        """Las tareas de sus proyectos con Wansoft, primero las que esperan algo de la persona.

        estado: abiertas (por defecto), esperan_mi_respuesta, esperan_mi_validacion, esperan_algo_mio, en_curso, sin_iniciar, hechas, canceladas o todas.
        proyecto_id: para ver un solo proyecto (de wansoft_mis_proyectos).
        limite: hasta 50.
        """
        return await api.llamar('mis_tareas', estado=estado, proyecto_id=proyecto_id, limite=limite)

    @app.tool(name='wansoft_ver_tarea', title='Ver una tarea de Wansoft', annotations=LECTURA)
    async def wansoft_ver_tarea(tarea_id: int) -> dict:
        """El detalle de una tarea: descripción, estado, responsables, fecha límite y la conversación con Wansoft."""
        return await api.llamar('ver_tarea', tarea_id=tarea_id)

    @app.tool(name='wansoft_mi_equipo', title='Mi equipo en Wansoft', annotations=LECTURA)
    async def wansoft_mi_equipo(proyecto_id: Optional[int] = None) -> dict:
        """Quién de Wansoft atiende cada módulo de sus proyectos. Da los rol_id y modulo_id para preparar una consulta."""
        return await api.llamar('mi_equipo', proyecto_id=proyecto_id)

    @app.tool(name='wansoft_preparar_consulta', title='Preparar una consulta a Wansoft', annotations=PREPARA)
    async def wansoft_preparar_consulta(proyecto_id: int, titulo: str, descripcion: str,
                                        rol_id: Optional[int] = None,
                                        modulo_id: Optional[int] = None) -> dict:
        """Prepara el borrador de una consulta para el equipo de Wansoft. NO crea nada.

        Mostrale el borrador a la persona y esperá que lo apruebe. Recién entonces usá wansoft_confirmar_consulta con el identificador, que vence a los 15 minutos.
        titulo: una frase que diga qué pasa. descripcion: qué pasa, dónde y qué se esperaba.
        rol_id y modulo_id: de wansoft_mi_equipo, obligatorios si el proyecto tiene equipo.
        """
        return await api.llamar('preparar_consulta', proyecto_id=proyecto_id, titulo=titulo,
                                descripcion=descripcion, rol_id=rol_id, modulo_id=modulo_id)

    @app.tool(name='wansoft_confirmar_consulta', title='Confirmar una consulta a Wansoft', annotations=CREA)
    async def wansoft_confirmar_consulta(identificador: str) -> dict:
        """Abre en Wansoft la consulta preparada. Usala SOLO después de que la persona vio el borrador y dijo que sí."""
        return await api.llamar('confirmar_consulta', identificador=identificador)
