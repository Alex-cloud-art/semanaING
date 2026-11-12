import os
import json
import time
import threading
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file, Response
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, KeepTogether, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False

import paho.mqtt.client as mqtt

from mqtt_config import MQTT_HOST, MQTT_PORT, T_SENSORES, T_ARDUINO, T_COMANDOS, T_ESTADO
from mqtt_broker import iniciar_broker_embebido
from arduino_bridge import ArduinoBridge

try:
    import google.generativeai as genai
    HAS_GEMINI_LIB = True
except ImportError:
    HAS_GEMINI_LIB = False

app = Flask(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
if HAS_GEMINI_LIB and GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Si tu sensor de flama da la lógica al revés, arranca con INVERTIR_FUEGO=1
INVERTIR_FUEGO = True  # Modificado por defecto para corregir lógica invertida del sensor de flama
# Segundos que se respetan los valores forzados desde los botones de simulación
SEGUNDOS_BLOQUEO_SIM = 15

mqtt_app = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pianitech-backend")
arduino_info = {"conectado": False, "puerto": None}
ultimo_dato_arduino = 0.0
bloqueo_sensores = {"agua": 0.0, "fuego": 0.0}
ultimo_cmd_paro = 0.0
webcam_habilitada = True

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(BASE_DIR, "evidence")
DB_INCIDENTES_FILE = os.path.join(BASE_DIR, "historial_incidentes.json")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

incidentes_persistentes = []
if os.path.exists(DB_INCIDENTES_FILE):
    try:
        with open(DB_INCIDENTES_FILE, "r", encoding="utf-8") as f:
            incidentes_persistentes = json.load(f)
    except Exception as e:
        print(f"[DB ERROR]: {e}")

tiempo_inicio_ausencia = None
simulacion_manual_activa = False
paro_registrado = False

system_state = {
    "modo_automatico": True,
    "webcam_activa": False,
    "webcam_habilitada": False,
    "webcam_error": "",
    "planta_info": {
        "empresa": "Pianitech Manufacturing Solutions",
        "planta": "Planta Ensamble y Maquinado Norte",
        "area": "Línea 3 - Celda Robotizada y Conformado Mecánico",
        "supervisor_turno": "Ing. A. Martínez / Turno Nocturno",
        "estacion": "Prensa y Desbaste Rotatorio #04",
        "normativa_referencia": "NOM-002-STPS-2010 / ISO 13849-1"
    },
    "case1": {
        "ventilador_activo": True,
        "operador_presente": True,
        "tiempo_sin_operador_seg": 0,
        "rpm_estimadas": 1450,
        "estado": "NORMAL",
        "decision_automatica": "Operador presente en celda. Máquina activa."
    },
    "case2": {
        "nivel_agua_porcentaje": 45.0,
        "valvula_llenado_abierta": True,
        "alerta_nivel": False,
        "estado": "LLENANDO",
        "decision_automatica": "Nivel dentro de margen nominal (45%). Válvula abierta."
    },
    "case3": {
        "temperatura_c": 28.5,
        "llama_detectada": False,
        "servo_aspersor_grados": 0,
        "estado": "NORMAL",
        "decision_automatica": "Temperatura ambiente controlada. Aspersor cerrado."
    },
    "case4": {
        "paro_emergencia_presionado": False,
        "voltaje_linea": 120.0,
        "corriente_a": 3.2,
        "estado": "OPTIMO",
        "decision_automatica": "Alimentación eléctrica nominal y enclavamiento seguro."
    },
    "incidentes": incidentes_persistentes
}

camera = None
output_frame = None
raw_frame = None
lock_frame = threading.Lock()
lock_incidentes = threading.Lock()

def guardar_historial_en_disco():
    try:
        with open(DB_INCIDENTES_FILE, "w", encoding="utf-8") as f:
            json.dump(system_state["incidentes"], f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[DISK ERROR]: {e}")

def enviar_comando_arduino(cmd):
    """Publica el comando en MQTT; el puente serial lo escribe en el Arduino."""
    try:
        mqtt_app.publish(T_COMANDOS, cmd, qos=1)
    except Exception as e:
        print(f"[MQTT ERROR]: {e}")

def arduino_vivo():
    return (time.time() - ultimo_dato_arduino) < 3.0

def publicar_estado():
    """Publica el estado completo del panel (retenido) para que el navegador lo reciba en tiempo real."""
    if not mqtt_app.is_connected():
        return
    snap = {k: v for k, v in system_state.items() if k != "incidentes"}
    snap["total_incidentes"] = len(system_state["incidentes"])
    snap["arduino"] = dict(arduino_info, datos_recientes=arduino_vivo())
    snap["ts"] = time.time()
    try:
        mqtt_app.publish(T_ESTADO, json.dumps(snap), qos=0, retain=True)
    except Exception as e:
        print(f"[MQTT ERROR]: {e}")

def procesar_sensores(d):
    """Aplica una lectura real del Arduino (llegada por MQTT) al estado del panel."""
    global ultimo_dato_arduino
    ultimo_dato_arduino = time.time()
    ahora = time.time()

    agua_val = int(d.get("agua", 0))
    hay_fuego = bool(d.get("fuego", False)) != INVERTIR_FUEGO
    motor = d.get("motor", None)
    arduino_info.update({"agua_raw": agua_val, "fuego": hay_fuego, "motor": motor})

    # Agua: valor analógico (0-1023, >500 es crítico) -> porcentaje (0-100%)
    if ahora >= bloqueo_sensores["agua"]:
        system_state["case2"]["nivel_agua_porcentaje"] = round(min(100.0, (agua_val / 550.0) * 100.0), 1)

    # Flama
    if ahora >= bloqueo_sensores["fuego"]:
        system_state["case3"]["llama_detectada"] = hay_fuego
        system_state["case3"]["temperatura_c"] = 65.0 if hay_fuego else 28.5

    # Ventilador reportado por el Arduino (botón físico o comando)
    if motor is not None and not simulacion_manual_activa:
        system_state["case1"]["ventilador_activo"] = bool(motor)
        system_state["case1"]["rpm_estimadas"] = 1450 if motor else 0

    publicar_estado()

def _on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    client.subscribe([(T_SENSORES, 0), (T_ARDUINO, 1)])
    print(f"[MQTT] Backend conectado al broker {MQTT_HOST}:{MQTT_PORT}")

def _on_mqtt_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        if msg.topic == T_SENSORES:
            procesar_sensores(data)
        elif msg.topic == T_ARDUINO:
            arduino_info["conectado"] = bool(data.get("conectado"))
            arduino_info["puerto"] = data.get("puerto")
            publicar_estado()
    except Exception as e:
        print(f"[MQTT MSG ERROR]: {e}")

mqtt_app.on_connect = _on_mqtt_connect
mqtt_app.on_message = _on_mqtt_message

def capturar_evidencia(tipo_incidente, detalle=""):
    global raw_frame, lock_frame
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_archivo = f"evidencia_{timestamp_str}_{int(time.time()*1000)%10000}.jpg"
    ruta_completa = os.path.join(EVIDENCE_DIR, nombre_archivo)

    captura_real = False
    with lock_frame:
        if raw_frame is not None:
            try:
                frame_copia = raw_frame.copy()
                cv2.rectangle(frame_copia, (0, 0), (640, 45), (15, 23, 42), -1)
                cv2.putText(frame_copia, f"PIANITECH | {tipo_incidente.upper()}", (15, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
                cv2.putText(frame_copia, datetime.now().strftime("%d/%m/%Y %H:%M:%S"), (430, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (148, 163, 184), 1)
                cv2.imwrite(ruta_completa, frame_copia)
                captura_real = True
            except Exception as e:
                print(f"[EVIDENCE ERROR]: {e}")

    if not captura_real and HAS_OPENCV:
        try:
            img = np.zeros((360, 600, 3), dtype=np.uint8)
            img[:] = (248, 246, 240)
            cv2.rectangle(img, (0, 0), (600, 50), (30, 58, 138), -1)
            cv2.putText(img, "PIANITECH - REGISTRO VISUAL PERICIAL", (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.rectangle(img, (20, 70), (580, 290), (226, 232, 240), -1)
            cv2.rectangle(img, (20, 70), (580, 290), (148, 163, 184), 1)
            cv2.putText(img, f"INCIDENTE: {tipo_incidente.upper()}", (40, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (185, 28, 28), 2)
            cv2.putText(img, f"MARCA TEMPORAL: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", (40, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (30, 41, 59), 1)
            cv2.putText(img, f"DETALLE: {detalle[:55]}", (40, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (30, 41, 59), 1)
            cv2.putText(img, "ESTADO: ACCION AUTOMATICA DE SEGURIDAD EJECUTADA", (40, 215), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (30, 58, 138), 2)
            cv2.rectangle(img, (40, 235), (280, 275), (220, 38, 38), 2)
            cv2.putText(img, "ENCLAVAMIENTO ACTIVO", (50, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 38, 38), 1)
            cv2.putText(img, "Evidencia documental generada para trazabilidad NOM-002-STPS.", (20, 330), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (100, 116, 139), 1)
            cv2.imwrite(ruta_completa, img)
        except Exception as e:
            print(f"[EVIDENCE SYNTHETIC ERROR]: {e}")

    return ruta_completa

def registrar_incidente(caso, tipo, descripcion, severidad, con_evidencia=True):
    with lock_incidentes:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        evidencia_path = capturar_evidencia(tipo, descripcion) if con_evidencia else None
        incidente = {
            "id": len(system_state["incidentes"]) + 1,
            "timestamp": now,
            "caso": caso,
            "tipo": tipo,
            "descripcion": descripcion,
            "severidad": severidad,
            "evidencia_foto": evidencia_path
        }
        system_state["incidentes"].append(incidente)
        guardar_historial_en_disco()
        print(f"[REGISTRO EXITOSO] #{incidente['id']} {caso} - {tipo}")
        return incidente

def vision_worker():
    """Webcam bajo demanda: se activa/desactiva desde la página; detección suavizada."""
    global camera, output_frame, raw_frame, simulacion_manual_activa
    if not HAS_OPENCV:
        system_state["webcam_error"] = "OpenCV no instalado"
        return
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    ultimo_visto = 0.0
    estaba_activa = False

    while True:
        system_state["webcam_habilitada"] = webcam_habilitada
        if not webcam_habilitada:
            if camera is not None:
                camera.release(); camera = None
            system_state["webcam_activa"] = False
            system_state["webcam_error"] = ""
            with lock_frame:
                output_frame = None; raw_frame = None
            estaba_activa = False
            time.sleep(0.3); continue

        if camera is None:
            for idx in (0, 1, 2):
                cam = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
                if cam.isOpened():
                    camera = cam; break
                cam.release()
            if camera is None:
                system_state["webcam_activa"] = False
                system_state["webcam_error"] = "No se encontró webcam (ciérrala en otras apps)"
                time.sleep(2); continue
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640); camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            system_state["webcam_error"] = ""

        ok, frame = camera.read()
        if not ok:
            camera.release(); camera = None; time.sleep(0.5); continue
        system_state["webcam_activa"] = True
        if not estaba_activa:
            ultimo_visto = time.time(); estaba_activa = True   # periodo de gracia al encender

        frame = cv2.flip(frame, 1)
        with lock_frame:
            raw_frame = frame.copy()

        gray = cv2.equalizeHist(cv2.cvtColor(cv2.resize(frame, (320, 240)), cv2.COLOR_BGR2GRAY))
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(28, 28))
        if len(faces) > 0:
            ultimo_visto = time.time()
            simulacion_manual_activa = False
        presente = (time.time() - ultimo_visto) < 1.5      # tolera parpadeos de detección
        if len(faces) > 0 or not simulacion_manual_activa:
            system_state["case1"]["operador_presente"] = presente

        for (x, y, w, h) in faces:
            x, y, w, h = x * 2, y * 2, w * 2, h * 2
            cv2.rectangle(frame, (x, y), (x + w, y + h), (34, 197, 94), 2)
            cv2.putText(frame, "OPERADOR EN PUESTO", (x, max(y - 8, 15)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (34, 197, 94), 2)
        pres = system_state["case1"]["operador_presente"]
        txt = "PRESENTE" if pres else f"AUSENTE ({system_state['case1']['tiempo_sin_operador_seg']}s)"
        cv2.putText(frame, f"Operador: {txt}", (14, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (34, 197, 94) if pres else (38, 38, 220), 2)
        with lock_frame:
            output_frame = frame.copy()
        time.sleep(0.03)

def loop_control_autonomo():
    global tiempo_inicio_ausencia, paro_registrado, ultimo_cmd_paro

    ultimo_pub = 0.0
    while True:
        try:
            if system_state["modo_automatico"]:
                c1 = system_state["case1"]

                # CASO 1: Lógica de paro y contador ininterrumpible
                # El botón físico manda sobre el ventilador; la cámara solo detiene por ausencia
                # y rearma únicamente si fue ella quien detuvo la máquina.
                if not c1["operador_presente"]:
                    if tiempo_inicio_ausencia is None:
                        tiempo_inicio_ausencia = time.time()

                    segundos = int(time.time() - tiempo_inicio_ausencia)
                    c1["tiempo_sin_operador_seg"] = segundos

                    if segundos >= 10:
                        c1["decision_automatica"] = f"Operador ausente ({segundos}s >= 10s). Máquina detenida automáticamente."
                        if not paro_registrado:
                            c1["ventilador_activo"] = False
                            c1["rpm_estimadas"] = 0
                            c1["estado"] = "APAGADO_PREVENTIVO"
                            paro_registrado = True
                            ultimo_cmd_paro = time.time()
                            enviar_comando_arduino("0") # ENVÍA PARO FÍSICO A ARDUINO
                            registrar_incidente("Caso 1", "Operador Ausente", f"Paro preventivo tras {segundos}s de desatención", "MEDIA", con_evidencia=True)
                        elif c1["ventilador_activo"] and time.time() - ultimo_cmd_paro > 1.0:
                            # Enclavamiento: si alguien lo enciende con el botón sin operador, se vuelve a cortar
                            c1["ventilador_activo"] = False
                            c1["rpm_estimadas"] = 0
                            ultimo_cmd_paro = time.time()
                            enviar_comando_arduino("0")
                    else:
                        c1["decision_automatica"] = f"Puesto desatendido ({segundos}/10s). Contando para paro de seguridad..."
                else:
                    tiempo_inicio_ausencia = None
                    c1["tiempo_sin_operador_seg"] = 0

                    if paro_registrado:
                        c1["ventilador_activo"] = True
                        c1["rpm_estimadas"] = 1450
                        c1["estado"] = "NORMAL"
                        paro_registrado = False
                        c1["decision_automatica"] = "Operador presente. Máquina rotatoria reactivada automáticamente."
                        enviar_comando_arduino("1") # ENVÍA ARRANQUE FÍSICO A ARDUINO
                        registrar_incidente("Caso 1", "Retorno de Operador", "Máquina rotatoria reactivada por presencia", "BAJA", con_evidencia=True)
                    elif c1["ventilador_activo"]:
                        c1["estado"] = "NORMAL"
                        c1["decision_automatica"] = "Operador presente. Ciclo rotatorio nominal en ejecución."
                    else:
                        c1["estado"] = "DETENIDO_MANUAL"
                        c1["decision_automatica"] = "Operador presente. Ventilador detenido manualmente."

                # CASO 2: Tanque de Agua (Simulado en caso de no Arduino, Real si hay Arduino)
                c2 = system_state["case2"]
                if not arduino_vivo():
                    if c2["valvula_llenado_abierta"]:
                        c2["nivel_agua_porcentaje"] = round(min(100.0, c2["nivel_agua_porcentaje"] + 0.2), 1)
                    else:
                        c2["nivel_agua_porcentaje"] = round(max(20.0, c2["nivel_agua_porcentaje"] - 0.15), 1)

                if c2["nivel_agua_porcentaje"] >= 90.0 and c2["valvula_llenado_abierta"]:
                    c2["valvula_llenado_abierta"] = False
                    c2["alerta_nivel"] = True
                    c2["estado"] = "BLOQUEO_LLENADO"
                    c2["decision_automatica"] = f"Nivel crítico ({c2['nivel_agua_porcentaje']}%). Válvula cerrada."
                    registrar_incidente("Caso 2", "Sobrellenado", f"Corte automático de válvula al {c2['nivel_agua_porcentaje']}%", "ALTA", con_evidencia=True)
                elif c2["nivel_agua_porcentaje"] >= 75.0:
                    c2["alerta_nivel"] = True
                    c2["estado"] = "ADVERTENCIA"
                    c2["decision_automatica"] = f"Nivel alto ({c2['nivel_agua_porcentaje']}%). Próximo a corte."
                elif c2["nivel_agua_porcentaje"] <= 30.0 and not c2["valvula_llenado_abierta"]:
                    c2["valvula_llenado_abierta"] = True
                    c2["alerta_nivel"] = False
                    c2["estado"] = "LLENANDO"
                    c2["decision_automatica"] = "Nivel bajo restablecido. Válvula abierta."

                # CASO 3: Térmico
                c3 = system_state["case3"]
                if (c3["llama_detectada"] or c3["temperatura_c"] >= 60.0) and c3["servo_aspersor_grados"] != 90:
                    c3["servo_aspersor_grados"] = 90
                    c3["estado"] = "EXTINCION_ACTIVA"
                    c3["decision_automatica"] = f"Fuego detectado ({c3['temperatura_c']}°C). Servomotor abierto a 90°."
                    enviar_comando_arduino("F")
                    registrar_incidente("Caso 3", "Conato de Fuego", f"Apertura de servomotor por fuego ({c3['temperatura_c']}°C)", "CRITICA", con_evidencia=True)

        except Exception as e:
            print(f"[LOOP ERROR] Evitado colapso de thread principal: {e}")
        if time.time() - ultimo_pub >= 0.5:
            ultimo_pub = time.time()
            publicar_estado()
        time.sleep(0.2)

def iniciar_servicios():
    iniciar_broker_embebido()
    mqtt_app.connect_async(MQTT_HOST, MQTT_PORT)
    mqtt_app.loop_start()
    ArduinoBridge().start()
    threading.Thread(target=vision_worker, daemon=True).start()
    threading.Thread(target=loop_control_autonomo, daemon=True).start()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/clasico")
def index_clasico():
    return render_template("index_clasico.html")

@app.route("/api/webcam", methods=["POST"])
def toggle_webcam():
    global webcam_habilitada, tiempo_inicio_ausencia, paro_registrado
    webcam_habilitada = bool((request.json or {}).get("activa", False))
    system_state["webcam_habilitada"] = webcam_habilitada
    if not webcam_habilitada:            # sin cámara el operador se da por presente (modo manual)
        system_state["case1"]["operador_presente"] = True
        system_state["case1"]["tiempo_sin_operador_seg"] = 0
        tiempo_inicio_ausencia = None
    publicar_estado()
    return jsonify({"status": "ok", "webcam_habilitada": webcam_habilitada})

@app.route("/api/telemetria", methods=["GET"])
def get_telemetria():
    return jsonify(dict(system_state, arduino=dict(arduino_info, datos_recientes=arduino_vivo())))

def generate_video_stream():
    global output_frame, lock_frame
    while True:
        with lock_frame:
            if output_frame is None:
                if not webcam_habilitada:
                    return
                time.sleep(0.04)
                continue
            ret, encoded_image = cv2.imencode('.jpg', output_frame)
            if not ret:
                continue
            byte_array = encoded_image.tobytes()

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + byte_array + b'\r\n')
        time.sleep(0.04)

@app.route("/video_feed")
def video_feed():
    if not HAS_OPENCV:
        return "OpenCV no disponible", 404
    return Response(generate_video_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route("/api/forzar_evento", methods=["POST"])
def forzar_evento():
    global tiempo_inicio_ausencia, simulacion_manual_activa, paro_registrado
    data = request.json or {}
    caso = data.get("caso")

    if caso == "iniciar_conteo_ausencia":
        simulacion_manual_activa = True
        system_state["case1"]["operador_presente"] = False
        tiempo_inicio_ausencia = time.time()
        paro_registrado = False
        system_state["case1"]["tiempo_sin_operador_seg"] = 0
        system_state["case1"]["decision_automatica"] = "Simulación manual de salida iniciada. Cronómetro corriendo..."

    elif caso == "retirar_operador":
        simulacion_manual_activa = True
        system_state["case1"]["operador_presente"] = False
        tiempo_inicio_ausencia = time.time() - 11
        system_state["case1"]["tiempo_sin_operador_seg"] = 11
        system_state["case1"]["ventilador_activo"] = False
        system_state["case1"]["rpm_estimadas"] = 0
        system_state["case1"]["estado"] = "APAGADO_PREVENTIVO"
        paro_registrado = True
        system_state["case1"]["decision_automatica"] = "Paro preventivo directo ejecutado (>10s)."
        enviar_comando_arduino("0")
        registrar_incidente("Caso 1", "Operador Ausente", "Paro preventivo directo de máquina rotatoria", "MEDIA", con_evidencia=True)

    elif caso == "ingresar_operador":
        simulacion_manual_activa = False
        tiempo_inicio_ausencia = None
        paro_registrado = False
        system_state["case1"]["operador_presente"] = True
        system_state["case1"]["tiempo_sin_operador_seg"] = 0
        system_state["case1"]["ventilador_activo"] = True
        system_state["case1"]["rpm_estimadas"] = 1450
        system_state["case1"]["estado"] = "NORMAL"
        system_state["case1"]["decision_automatica"] = "Operador presente. Máquina rotatoria reactivada."
        enviar_comando_arduino("1")
        registrar_incidente("Caso 1", "Retorno de Operador", "Reactivación automática por retorno de operador", "BAJA", con_evidencia=True)

    elif caso == "inducir_fuego":
        bloqueo_sensores["fuego"] = time.time() + SEGUNDOS_BLOQUEO_SIM
        system_state["case3"]["temperatura_c"] = 68.0
        system_state["case3"]["llama_detectada"] = True
        system_state["case3"]["servo_aspersor_grados"] = 90
        system_state["case3"]["estado"] = "EXTINCION_ACTIVA"
        system_state["case3"]["decision_automatica"] = "Fuego inducido (68°C). Servomotor abierto a 90°."
        enviar_comando_arduino("F")
        registrar_incidente("Caso 3", "Conato de Fuego", "Llama y temperatura de 68°C detectada. Extinción activa", "CRITICA", con_evidencia=True)

    elif caso == "apagar_fuego":
        bloqueo_sensores["fuego"] = time.time() + SEGUNDOS_BLOQUEO_SIM
        system_state["case3"]["temperatura_c"] = 28.0
        system_state["case3"]["llama_detectada"] = False
        system_state["case3"]["servo_aspersor_grados"] = 0
        system_state["case3"]["estado"] = "NORMAL"
        system_state["case3"]["decision_automatica"] = "Temperatura normalizada (28°C). Servomotor en 0°."
        enviar_comando_arduino("R")
        registrar_incidente("Caso 3", "Fuego Controlado", "Normalización térmica y rearmado de aspersor a 0°", "BAJA", con_evidencia=True)

    elif caso == "forzar_nivel_tanque":
        val = float(data.get("valor", 95.0))
        bloqueo_sensores["agua"] = time.time() + SEGUNDOS_BLOQUEO_SIM
        system_state["case2"]["nivel_agua_porcentaje"] = val
        if val >= 90.0:
            system_state["case2"]["valvula_llenado_abierta"] = False
            system_state["case2"]["alerta_nivel"] = True
            system_state["case2"]["estado"] = "BLOQUEO_LLENADO"
            system_state["case2"]["decision_automatica"] = f"Nivel crítico ({val}%). Válvula de llenado cerrada."
            registrar_incidente("Caso 2", "Sobrellenado", f"Nivel crítico ({val}%). Válvula cerrada", "ALTA", con_evidencia=True)
        else:
            system_state["case2"]["valvula_llenado_abierta"] = True
            system_state["case2"]["alerta_nivel"] = False
            system_state["case2"]["estado"] = "LLENANDO"
            system_state["case2"]["decision_automatica"] = f"Nivel nominal ({val}%). Válvula abierta."
            registrar_incidente("Caso 2", "Restablecimiento de Nivel", f"Tanque en nivel operativo ({val}%)", "BAJA", con_evidencia=True)

    publicar_estado()
    return jsonify({"status": "ok", "state": system_state})

@app.route("/api/limpiar_historial", methods=["POST"])
def limpiar_historial():
    with lock_incidentes:
        system_state["incidentes"] = []
        guardar_historial_en_disco()
    return jsonify({"status": "ok", "total": 0})

@app.route("/api/generar_analisis_ia", methods=["POST"])
def generar_analisis_ia():
    incidentes = system_state["incidentes"]
    num = len(incidentes)

    fuego = any("Fuego" in i["tipo"] for i in incidentes)
    agua = any("Sobrellenado" in i["tipo"] for i in incidentes)
    ausencia = any("Ausente" in i["tipo"] for i in incidentes)

    res = f"**DICTAMEN DE AUDITORÍA PIANITECH ({num} eventos en bitácora):**\n\n"

    if ausencia:
        res += "• **Caso 1 (Rotatorio):** Se auditó el paro preventivo automático por ausencia del operador en la celda y la reactivación segura inmediata.\n"
    else:
        res += "• **Caso 1 (Rotatorio):** Operación nominal, sin reportes de ausencia prolongada.\n"

    if agua:
        res += "• **Caso 2 (Tanque):** Cierre de seguridad de la válvula ejecutado de forma autónoma al rebasar el 90%.\n"
    else:
        res += "• **Caso 2 (Tanque):** Nivel controlado dentro del umbral de seguridad (<90%).\n"

    if fuego:
        res += "• **Caso 3 (Extinción):** Despliegue del servomotor de aspersión a 90° ante gradiente térmico crítico.\n\n"
    else:
        res += "• **Caso 3 (Extinción):** Parámetros térmicos estables, sin detección de ignición.\n\n"

    res += "**Recomendaciones de Ingeniería:**\n"
    res += "1. Las fotos de evidencia pericial adjuntas en el reporte validan la trazabilidad ante brigadas internas.\n"
    res += "2. Mantener la alineación de la cámara para asegurar una línea de visión sin oclusiones."

    return jsonify({"resultado_ia": res})

@app.route("/api/descargar_reporte_pdf", methods=["GET"])
def descargar_reporte_pdf():
    if not system_state["incidentes"]:
        registrar_incidente("Caso 1", "Operador Ausente", "Demostración: puesto desatendido y paro preventivo", "MEDIA", con_evidencia=True)

    pdf_filename = f"reporte_pianitech_{int(time.time())}.pdf"
    ruta_pdf = os.path.join(EVIDENCE_DIR, pdf_filename)
    doc = SimpleDocTemplate(ruta_pdf, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    encabezado_style = ParagraphStyle('Encabezado', fontName='Helvetica-Bold', fontSize=15, textColor=colors.HexColor('#1E3A8A'), spaceAfter=2)
    subencabezado_style = ParagraphStyle('SubEncabezado', fontName='Helvetica', fontSize=8, textColor=colors.HexColor('#475569'), spaceAfter=10)
    seccion_style = ParagraphStyle('Seccion', fontName='Helvetica-Bold', fontSize=10, textColor=colors.HexColor('#0F172A'), spaceBefore=8, spaceAfter=4)
    celda_header = ParagraphStyle('CH', fontName='Helvetica-Bold', fontSize=7.5, textColor=colors.white)
    celda_body = ParagraphStyle('CB', fontName='Helvetica', fontSize=7, textColor=colors.HexColor('#1E293B'))

    story.append(Paragraph("PIANITECH INDUSTRIAL · INFORME PERICIAL Y AUDITORÍA DE SEGURIDAD", encabezado_style))
    story.append(Paragraph("DOCUMENTO DE TRAZABILIDAD OPERACIONAL Y RESPUESTA AUTÓNOMA EN MAQUINARIA", subencabezado_style))

    info_planta = [
        [Paragraph("<b>Planta:</b> " + system_state["planta_info"]["planta"], celda_body),
         Paragraph("<b>Fecha y Hora:</b> " + datetime.now().strftime('%d/%m/%Y %H:%M:%S'), celda_body)],
        [Paragraph("<b>Área Operativa:</b> " + system_state["planta_info"]["area"], celda_body),
         Paragraph("<b>Supervisor en Turno:</b> " + system_state["planta_info"]["supervisor_turno"], celda_body)],
        [Paragraph("<b>Estación Auditada:</b> " + system_state["planta_info"]["estacion"], celda_body),
         Paragraph("<b>Norma de Cumplimiento:</b> " + system_state["planta_info"]["normativa_referencia"], celda_body)]
    ]
    t_planta = Table(info_planta, colWidths=[270, 270])
    t_planta.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F0F9FF')),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#BAE6FD')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E0F2FE')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t_planta)
    story.append(Spacer(1, 8))

    story.append(Paragraph("1. Telemetría y Acciones Autónomas Registradas", seccion_style))
    c1 = system_state["case1"]
    c2 = system_state["case2"]
    c3 = system_state["case3"]
    c4 = system_state["case4"]

    telemetria_data = [
        [Paragraph("Estación", celda_header), Paragraph("Variable Principal", celda_header), Paragraph("Actuador", celda_header), Paragraph("Estado", celda_header), Paragraph("Acción Autónoma Ejecutada", celda_header)],
        [Paragraph("1: Máquina Rotatoria", celda_body), Paragraph(f"Presencia: {'DETECTADO' if c1['operador_presente'] else 'AUSENTE'}", celda_body), Paragraph(f"Ventilador: {'ON' if c1['ventilador_activo'] else 'OFF'}", celda_body), Paragraph(c1["estado"], celda_body), Paragraph(c1["decision_automatica"], celda_body)],
        [Paragraph("2: Tanque de Agua", celda_body), Paragraph(f"Nivel: {c2['nivel_agua_porcentaje']}%", celda_body), Paragraph(f"Válvula: {'ABIERTA' if c2['valvula_llenado_abierta'] else 'CERRADA'}", celda_body), Paragraph(c2["estado"], celda_body), Paragraph(c2["decision_automatica"], celda_body)],
        [Paragraph("3: Extinción Térmica", celda_body), Paragraph(f"Temp: {c3['temperatura_c']} °C", celda_body), Paragraph(f"Servo: {c3['servo_aspersor_grados']}°", celda_body), Paragraph(c3["estado"], celda_body), Paragraph(c3["decision_automatica"], celda_body)],
        [Paragraph("4: Suministro Eléctrico", celda_body), Paragraph(f"Tensión: {c4['voltaje_linea']} V", celda_body), Paragraph("Relé de Línea: ACTIVO", celda_body), Paragraph(c4["estado"], celda_body), Paragraph(c4["decision_automatica"], celda_body)]
    ]
    t_tel = Table(telemetria_data, colWidths=[90, 85, 80, 85, 200])
    t_tel.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E40AF')),
        ('BOX', (0,0), (-1,-1), 0.8, colors.HexColor('#CBD5E1')),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F8FAFC')]),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ]))
    story.append(t_tel)
    story.append(Spacer(1, 8))

    story.append(Paragraph(f"2. Registro Cronológico Completo de Incidentes ({len(system_state['incidentes'])} eventos)", seccion_style))
    if system_state["incidentes"]:
        inc_data = [[Paragraph("Hora", celda_header), Paragraph("Estación", celda_header), Paragraph("Tipo", celda_header), Paragraph("Severidad", celda_header), Paragraph("Descripción de la Intervención", celda_header)]]
        for inc in system_state["incidentes"]:
            inc_data.append([
                Paragraph(inc["timestamp"], celda_body),
                Paragraph(inc["caso"], celda_body),
                Paragraph(inc["tipo"], celda_body),
                Paragraph(inc["severidad"], celda_body),
                Paragraph(inc["descripcion"], celda_body)
            ])
        t_inc = Table(inc_data, colWidths=[80, 65, 85, 60, 250], repeatRows=1)
        t_inc.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#B91C1C')),
            ('BOX', (0,0), (-1,-1), 0.8, colors.HexColor('#CBD5E1')),
            ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#FEF2F2')]),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ]))
        story.append(t_inc)
    else:
        story.append(Paragraph("<i>No hay eventos registrados en bitácora.</i>", celda_body))
    story.append(Spacer(1, 10))

    story.append(PageBreak())

    story.append(Paragraph("3. Evidencia Fotográfica y Registro Visual Forense", seccion_style))
    evidencias_con_foto = [inc for inc in system_state["incidentes"] if inc.get("evidencia_foto") and os.path.exists(inc["evidencia_foto"])]

    if evidencias_con_foto:
        for idx, inc in enumerate(evidencias_con_foto[-6:]):
            try:
                img_element = RLImage(inc["evidencia_foto"], width=220, height=130)
                pie_foto = Paragraph(f"<b>Evidencia #{idx+1}:</b> {inc['tipo']}<br/><b>Estación:</b> {inc['caso']}<br/><b>Hora:</b> {inc['timestamp']}<br/><b>Severidad:</b> {inc['severidad']}<br/><b>Detalle:</b> {inc['descripcion']}", celda_body)
                tabla_evidencia = Table([[img_element, pie_foto]], colWidths=[230, 310])
                tabla_evidencia.setStyle(TableStyle([
                    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                    ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
                    ('BACKGROUND', (0,0), (-1,-1), colors.HexColor('#F8FAFC')),
                    ('LEFTPADDING', (0,0), (-1,-1), 6),
                    ('RIGHTPADDING', (0,0), (-1,-1), 6),
                    ('TOPPADDING', (0,0), (-1,-1), 4),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ]))
                story.append(KeepTogether([tabla_evidencia, Spacer(1, 6)]))
            except Exception as e:
                print(f"[PDF IMAGE ERROR]: {e}")
    else:
        story.append(Paragraph("<i>No se localizaron archivos fotográficos en disco.</i>", celda_body))

    story.append(Spacer(1, 12))
    firmas_data = [
        [Paragraph("____________________________<br/><b>Ing. Supervisor de Planta</b><br/>Área de Seguridad e Higiene", celda_body),
         Paragraph("____________________________<br/><b>Operador Certificado</b><br/>Estación Mecanizada Pianitech", celda_body)]
    ]
    t_firmas = Table(firmas_data, colWidths=[270, 270])
    t_firmas.setStyle(TableStyle([
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'TOP')
    ]))
    story.append(KeepTogether(t_firmas))

    doc.build(story)
    return send_file(ruta_pdf, as_attachment=True)

if __name__ == "__main__":
    iniciar_servicios()
    print("\n>>> Panel Pianitech:  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
