# Pianitech · Arduino → MQTT → Panel web (tiempo real)

## Arquitectura
```
Arduino ──USB──► Puente serial ──MQTT──► Broker (embebido) ──► Backend Flask (lógica/incidentes/PDF)
 (sensores)      arduino_bridge.py       tcp:1883 / ws:9001            │
      ▲                                        ▲                       │ pianitech/estado (retenido)
      └── comandos '0' '1' 'F' 'R' ◄── pianitech/comandos ◄────────────┤
                                               └──► Navegador (mqtt.js por WebSocket)
```
| Tópico | Sentido | Contenido |
|---|---|---|
| `pianitech/sensores` | puente → backend | `{agua, fuego, motor, ts}` |
| `pianitech/enlace_usb` | puente → backend | `{conectado, puerto}` (retenido) |
| `pianitech/comandos` | backend → puente → Arduino | `0` paro, `1` arranque, `F`, `R` |
| `pianitech/estado` | backend → navegador | estado completo del panel (retenido) |

## Uso
1. Sube `firmware/sketch_pianitech.ino` al Arduino (cierra el Monitor Serie del IDE).
2. Conéctalo por USB y ejecuta **`run.bat`** (Windows) o `./run.sh`.
3. Abre http://localhost:5000

El puerto se detecta solo. Si no, define `ARDUINO_PORT` (ej. `COM3`).
Si tu sensor de flama trabaja al revés: `INVERTIR_FUEGO=1`.
Si ya tienes Mosquitto en el puerto 1883 se reutiliza (necesita listener WebSocket en 9001).

La página nueva está en `/` y la anterior en `/clasico`. La webcam se activa con el interruptor de la tarjeta "Operador".

Sin Arduino conectado el panel sigue funcionando en modo simulación, como antes.
