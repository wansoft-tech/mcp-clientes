"""Arranca el intermedio. Toda la configuración llega por variables de entorno."""
import logging
import os

import uvicorn

from .app import armar_app


def main():
    logging.basicConfig(level=logging.INFO)
    # PROVISIONER_PORT y no PORT: Easypanel le pone PORT=80 a toda app, y el
    # dominio y el chequeo de salud esperan el 8000.
    uvicorn.run(
        armar_app(),
        host=os.environ.get('PROVISIONER_HOST', '0.0.0.0'),
        port=int(os.environ.get('PROVISIONER_PORT', '8000')),
        proxy_headers=True,
        forwarded_allow_ips='*',
    )


if __name__ == '__main__':
    main()
