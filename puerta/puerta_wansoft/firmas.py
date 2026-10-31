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


def clave_del_enlace(secreto: bytes, conexion_id: int) -> str:
    """La clave del servidor de una persona, derivada del secreto compartido con Odoo.

    Odoo la calcula igual al pedir el servidor y manda solo su huella al
    intermedio. Así la puerta puede llegar al servidor de cada persona sin que
    nadie guarde ni muestre su clave.
    """
    return _b64(hmac.new(secreto, b'enlace:%d' % int(conexion_id), hashlib.sha256).digest())


def firma_hex(secreto: bytes, fecha: str, cuerpo: bytes) -> str:
    """Firma de un pedido entre la puerta y Odoo, como la del intermedio."""
    return hmac.new(secreto, fecha.encode() + b'.' + cuerpo, hashlib.sha256).hexdigest()
