"""Arranca el intermedio. Toda la configuración llega por variables de entorno."""
import logging
import os

import uvicorn

from .app import armar_app


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        armar_app(),
        host=os.environ.get('HOST', '0.0.0.0'),
        port=int(os.environ.get('PORT', '8000')),
        proxy_headers=True,
        forwarded_allow_ips='*',
    )


if __name__ == '__main__':
    main()
