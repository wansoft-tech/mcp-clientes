"""Arranca el servidor de una persona. Toda la configuración llega por variables de entorno."""
import logging
import os
import sys
import time

import uvicorn

from .app import armar_app, armar_servidor

_logger = logging.getLogger(__name__)


def _pausa_antes_de_salir():
    """Si el servidor no arranca, espera antes de salir.

    Sin esto, un servidor que no puede entrar a su Odoo (clave vencida o
    borrada, base que ya no existe) sale en un segundo, Docker lo vuelve a
    levantar enseguida y el bucle se come un núcleo de CPU entero. Varios así
    tiraron el servidor de Easypanel el 26/11/2026. Con la pausa, se reintenta
    una vez cada MCP_PAUSA_FALLO segundos (10 minutos por defecto).
    """
    try:
        segundos = int(os.environ.get('MCP_PAUSA_FALLO', '600'))
    except ValueError:
        segundos = 600
    print('No se pudo arrancar: se reintenta en %d segundos. Revisá la dirección, la base, '
          'el usuario y la clave de API.' % segundos, file=sys.stderr, flush=True)
    time.sleep(max(segundos, 0))


def main():
    try:
        servidor = armar_servidor()
        uvicorn.run(
            armar_app(servidor),
            host=servidor.config.host,
            port=servidor.config.port,
            # Los logs de acceso de uvicorn llevan la ruta, y la ruta lleva la
            # clave del enlace: no se registran.
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips='*',
        )
    except SystemExit as salida:
        # uvicorn sale con 3 cuando falla el arranque (no pudo entrar a Odoo).
        if salida.code not in (0, None):
            _pausa_antes_de_salir()
        raise
    except Exception:
        _logger.exception('Falló el arranque')
        _pausa_antes_de_salir()
        sys.exit(3)


if __name__ == '__main__':
    main()
