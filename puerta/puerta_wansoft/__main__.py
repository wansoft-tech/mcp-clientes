import os

import uvicorn

from .app import armar


def main():
    uvicorn.run(armar(), host='0.0.0.0', port=int(os.environ.get('PUERTA_PORT', '8000')),
                proxy_headers=True, forwarded_allow_ips='*', access_log=False)


if __name__ == '__main__':
    main()
