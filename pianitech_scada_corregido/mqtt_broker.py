"""Broker MQTT embebido (amqtt): TCP 1883 + WebSocket 9001.

Si ya hay un broker escuchando en el puerto MQTT (p. ej. Mosquitto), se usa ese
y no se levanta el embebido.
"""
import asyncio
import logging
import socket
import threading

from mqtt_config import MQTT_BIND, MQTT_HOST, MQTT_PORT, MQTT_WS_PORT

logging.getLogger("amqtt").setLevel(logging.ERROR)
logging.getLogger("transitions").setLevel(logging.ERROR)


def _puerto_abierto(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def iniciar_broker_embebido():
    if _puerto_abierto(MQTT_HOST, MQTT_PORT):
        print(f"[MQTT] Ya hay un broker en {MQTT_HOST}:{MQTT_PORT}; se reutiliza.")
        return True

    try:
        from amqtt.broker import Broker
    except ImportError:
        print("[MQTT] Falta 'amqtt' (pip install amqtt) y no hay broker externo.")
        return False

    config = {
        "listeners": {
            "default": {"type": "tcp", "bind": f"{MQTT_BIND}:{MQTT_PORT}"},
            "ws": {"type": "ws", "bind": f"{MQTT_BIND}:{MQTT_WS_PORT}"},
        },
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
        },
    }

    listo = threading.Event()

    async def _run():
        broker = Broker(config)
        await broker.start()
        listo.set()
        await asyncio.Event().wait()

    def _thread():
        try:
            asyncio.run(_run())
        except Exception as e:  # noqa
            print(f"[MQTT] El broker embebido se detuvo: {e}")

    threading.Thread(target=_thread, daemon=True, name="broker-mqtt").start()

    if listo.wait(timeout=10):
        print(f"[MQTT] Broker embebido listo  ->  tcp:{MQTT_PORT}  ws:{MQTT_WS_PORT}")
        return True
    print("[MQTT] No se pudo levantar el broker embebido.")
    return False
