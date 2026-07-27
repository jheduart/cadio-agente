import os
import uuid
import datetime
import traceback
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import create_cadio_agent
from app.tools import get_firestore_client, MOCK_ACTIVE_SESSIONS

app = FastAPI(title="Cadio Agente API", version="1.0.0")

# Configurar variables de entorno y fallbacks
PROJECT_ID = os.environ.get("PROJECT_ID", "proveedores-dev")
CODIO_API_URL = os.environ.get("CODIO_API_URL", "http://localhost:8080")
DEFAULT_CLIENT_WEBHOOK_URL = os.environ.get("DEFAULT_CLIENT_WEBHOOK_URL")

print(f"[STARTUP] Inicializando Cadio Agente en FastAPI...")
print(f"[STARTUP] PROJECT_ID: {PROJECT_ID}")
print(f"[STARTUP] CODIO_API_URL: {CODIO_API_URL}")

# Inicializar componentes de ADK de forma global
try:
    cadio_agent = create_cadio_agent()
    session_service = InMemorySessionService()
    runner = Runner(agent=cadio_agent, app_name="cadio-agente", session_service=session_service)
    print("[STARTUP] Componentes de Google ADK inicializados exitosamente.")
except Exception as e:
    print(f"[STARTUP ERROR] Error inicializando componentes ADK: {e}")
    traceback.print_exc()

def resolve_active_session(phone: str, account_sid: str = "AC_default") -> str:
    """Verifica en Firestore si existe una sesión SaaS activa.
    
    Retorna el session_id activo o genera uno nuevo si no existe.
    """
    db = get_firestore_client()
    now = datetime.datetime.utcnow()
    
    session_id = None
    
    if db:
        try:
            contact_ref = db.collection("cuentas").document(account_sid).collection("contactos").document(phone)
            contact_doc = contact_ref.get()
            
            if contact_doc.exists:
                contact_data = contact_doc.to_dict()
                active_session_id = contact_data.get("sesionActivaId")
                if active_session_id:
                    # Verificar si la sesión existe y está abierta
                    session_ref = db.collection("cuentas").document(account_sid).collection("sesiones").document(active_session_id)
                    session_doc = session_ref.get()
                    if session_doc.exists:
                        session_data = session_doc.to_dict()
                        if session_data.get("estado") == "abierta":
                            session_id = active_session_id
                            print(f"[SESSION] Reutilizando sesión SaaS activa: {session_id} para {phone}")
            
            if not session_id:
                session_id = f"sess_{str(uuid.uuid4().hex)[:11]}"
                # Crear sesión en la ruta SaaS
                session_ref = db.collection("cuentas").document(account_sid).collection("sesiones").document(session_id)
                session_ref.set({
                    "contactoTelefono": phone,
                    "estado": "abierta",
                    "actividadGen": 1,
                    "abiertaAt": now,
                    "ultimaActividadAt": now,
                    "ultimoMensajeAt": now,
                    "ultimoMensajePreview": "[Creado por agente]",
                    "atencion": {
                        "modo": "bot",
                        "asesor_id": None,
                        "equipo_id": None,
                        "solicitado_at": None
                    }
                })
                # Actualizar puntero en el contacto
                contact_ref.set({
                    "sesionActivaId": session_id,
                    "ultimaInteraccionAt": now,
                    "telefono": phone
                }, merge=True)
                print(f"[SESSION] Nueva sesión SaaS creada: {session_id} para {phone}")
                
        except Exception as e:
            print(f"[SESSION ERROR] Error interactuando con Firestore para sesión SaaS: {e}. Usando fallback local.")
            db = None
            
    if not db:
        # Fallback local mock
        active_sess = MOCK_ACTIVE_SESSIONS.get(phone)
        if active_sess:
            session_id = active_sess["active_session_id"]
            print(f"[SESSION] Reutilizando sesión activa local: {session_id} para {phone}")
        
        if not session_id:
            session_id = f"sess_{str(uuid.uuid4().hex)[:11]}"
            MOCK_ACTIVE_SESSIONS[phone] = {
                "active_session_id": session_id,
                "updatedAt": now
            }
            print(f"[SESSION] Nueva sesión local creada: {session_id} para {phone}")
            
    return session_id

def update_session_timestamp(phone: str, session_id: str, account_sid: str = "AC_default"):
    """Actualiza la marca de tiempo de la última interacción de la sesión en Firestore o local."""
    db = get_firestore_client()
    now = datetime.datetime.utcnow()
    
    if db:
        try:
            # Ruta SaaS oficial de la sesión
            db.collection("cuentas").document(account_sid).collection("sesiones").document(session_id).set({
                "ultimaActividadAt": now,
                "ultimoMensajeAt": now
            }, merge=True)
        except Exception as e:
            print(f"[SESSION ERROR] No se pudo actualizar timestamp en Firestore SaaS: {e}")
    else:
        if phone in MOCK_ACTIVE_SESSIONS:
            MOCK_ACTIVE_SESSIONS[phone]["updatedAt"] = now

def send_outbound_message(phone: str, message: str, account_sid: str = "AC_default"):
    """Llama de forma síncrona a la API de salida de Codio para responder por WhatsApp."""
    url = f"{CODIO_API_URL}/api/v1/messages/send"
    payload = {
        "to": phone,
        "type": "text",
        "body": message,
        "accountSid": account_sid
    }
    
    print(f"[OUTBOUND] Enviando respuesta a Codio para: {phone} (Cuenta: {account_sid})...")
    try:
        response = requests.post(url, json=payload, headers={"x-account-sid": account_sid}, timeout=10)
        if response.status_code == 200:
            print(f"[OUTBOUND] Mensaje enviado exitosamente a través de Codio. Respuesta: {response.json()}")
        else:
            print(f"[OUTBOUND ERROR] La API de Codio respondió con código {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[OUTBOUND ERROR] No se pudo conectar a la API de salida de Codio: {e}")

async def process_agent_interaction(phone: str, message_body: str, session_id: str, account_sid: str = "AC_default"):
    """Maneja el procesamiento de la conversación con el ADK Runner en background."""
    try:
        # 1. Asegurar que la sesión existe en el InMemorySessionService de ADK
        session = await session_service.get_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
        if session is None:
            await session_service.create_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
            session = await session_service.get_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
        
        # 2. Inyectar variables críticas del contexto en el estado de sesión para las herramientas
        session.state["phone"] = phone
        session.state["session_id"] = session_id
        session.state["account_sid"] = account_sid
        
        # 3. Ejecutar el agente ADK de forma asíncrona
        response_text = ""
        print(f"[ADK] Ejecutando agente para {phone} en sesión {session_id} (Cuenta: {account_sid})...")
        
        new_msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text=message_body)]
        )
        
        async for event in runner.run_async(
            user_id=phone,
            session_id=session_id,
            new_message=new_msg
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    response_text = event.content.parts[0].text
        
        # 4. Verificar si la herramienta end_conversation marcó la sesión como cerrada
        is_closed = session.state.get("session_closed", False)
        if is_closed:
            print(f"[LIFECYCLE] La sesión {session_id} ha sido cerrada explícitamente.")
            # Borrar de forma segura la sesión del servicio en memoria de ADK para liberar memoria
            try:
                await session_service.delete_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
                # Borrado local en caso de fallback
                MOCK_ACTIVE_SESSIONS.pop(phone, None)
            except Exception as e:
                print(f"[SESSION ERROR] Error borrando sesión de ADK: {e}")
        else:
            # Si sigue abierta, actualizamos su marca de última interacción
            update_session_timestamp(phone, session_id, account_sid)
            
        # 5. Despachar el mensaje de salida de vuelta al usuario final a través de Codio
        if response_text:
            send_outbound_message(phone, response_text, account_sid)
        else:
            print("[ADK WARNING] El agente no generó respuesta de texto final.")
            
    except Exception as e:
        print(f"[PROCESS ERROR] Error crítico procesando interacción de IA para {phone}: {e}")
        traceback.print_exc()
        # Enviar mensaje de error fallback para evitar que el chat quede colgado
        send_outbound_message(
            phone, 
            "Disculpas, estoy experimentando dificultades técnicas temporales. Por favor, reintenta tu consulta en unos momentos. 🩺",
            account_sid
        )

@app.post("/webhook")
async def handle_codio_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receptor asíncrono de webhooks entrantes de la plataforma Codio."""
    try:
        payload = await request.json()
        print(f"[WEBHOOK] Recibido payload de webhook: {payload}")
        
        # Validar tipo de evento
        event_type = payload.get("event")
        if event_type != "message.received":
            print(f"[WEBHOOK WARNING] Ignorando evento no soportado: {event_type}")
            return {"status": "ignored", "reason": "event_type_not_supported"}
            
        data = payload.get("data", {})
        phone = data.get("phone")
        body_text = data.get("body")
        account_sid = data.get("accountSid", "AC_default")
        session_id = data.get("sessionId")
        
        if not phone or not body_text:
            print("[WEBHOOK ERROR] El payload no contiene campos 'phone' o 'body' válidos.")
            raise HTTPException(status_code=400, detail="Missing phone or body fields")
            
        if not session_id:
            # Fallback en caso de que no venga el session_id (ej. pruebas directas antiguas)
            session_id = resolve_active_session(phone)
        
        # Verificar el modo de atención de la sesión en Firestore
        db = get_firestore_client()
        if db:
            try:
                # Ruta SaaS oficial: cuentas/{account_sid}/sesiones/{session_id}
                sesion_ref = db.collection("cuentas").document(account_sid).collection("sesiones").document(session_id)
                sesion_doc = sesion_ref.get()
                if sesion_doc.exists:
                    sesion_data = sesion_doc.to_dict()
                    atencion = sesion_data.get("atencion", {})
                    modo = atencion.get("modo", "bot")
                    
                    if modo in ["en_atencion_humana", "esperando_asesor"]:
                        print(f"[WEBHOOK] Ignorando mensaje de {phone} (Sesión: {session_id}) porque está en modo: {modo}")
                        return {"status": "ignored", "reason": f"session_in_human_attention_{modo}"}
                    
                    # Asegurar que comience en modo bot si no tiene atencion inicializada
                    if "atencion" not in sesion_data:
                        sesion_ref.update({
                            "atencion": {
                                "modo": "bot",
                                "asesor_id": None,
                                "equipo_id": None,
                                "solicitado_at": None
                            }
                        })
            except Exception as e:
                print(f"[WEBHOOK WARNING] Error al consultar estado de atención en Firestore: {e}")
            
        # 2. Despachar el procesamiento pesado del Agente ADK a una tarea de fondo (asíncrona)
        # Esto permite responderle 200 OK inmediatamente al webhook de Codio
        background_tasks.add_task(process_agent_interaction, phone, body_text, session_id, account_sid)
        
        return {"status": "accepted", "session_id": session_id}
        
    except Exception as e:
        print(f"[WEBHOOK ERROR] Error procesando solicitud: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
def health_check():
    """Health check endpoint para Cloud Run."""
    return {
        "status": "UP",
        "service": "Cadio Agente Service",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

@app.get("/")
def home():
    """Ruta inicial de bienvenida."""
    return {"message": "Servicio de Cadio Agente con Google ADK activo y listo para procesar eventos."}
