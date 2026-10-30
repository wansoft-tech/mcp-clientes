"""Arranca el servidor de una persona. Toda la configuración llega por variables de entorno."""
import uvicorn

from .app import armar_app, armar_servidor


def main():
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


if __name__ == '__main__':
    main()
