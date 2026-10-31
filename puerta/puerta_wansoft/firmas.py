"""Datos firmados: la puerta no guarda códigos ni credenciales, los firma.

Formato: base64url(json) + '.' + base64url(HMAC-SHA256). Cada dato lleva su
tipo, para que un código no se pueda usar como credencial ni al revés, y su
vencimiento.
"""
import base64
import hashlib
import hmac
import json
import time


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b'=').decode()


def _desde_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + '=' * (-len(texto) % 4))


def firmar(secreto: bytes, tipo: str, datos: dict, segundos: int) -> str:
    cuerpo = dict(datos, t=tipo, exp=int(time.time()) + segundos)
    carga = _b64(json.dumps(cuerpo, separators=(',', ':'), sort_keys=True).encode())
    firma = _b64(hmac.new(secreto, carga.encode(), hashlib.sha256).digest())
    return '%s.%s' % (carga, firma)


def leer(secreto: bytes, tipo: str, texto: str) -> dict | None:
    """Los datos, si la firma es buena, el tipo coincide y no venció. Si no, None."""
    try:
        carga, firma = (texto or '').split('.')
        esperada = _b64(hmac.new(secreto, carga.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(firma, esperada):
            return None
        datos = json.loads(_desde_b64(carga))
    except (ValueError, TypeError):
        return None
    if datos.get('t') != tipo or datos.get('exp', 0) < time.time():
        return None
    return datos
