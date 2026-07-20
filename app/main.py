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

def resolve_active_session(phone: str) -> str:
    """Verifica en Firestore (o local mock) si existe una sesión activa de menos de 24hs.
    
    Retorna el session_id activo o genera uno nuevo si expiró o no existe.
    """
    db = get_firestore_client()
    now = datetime.datetime.utcnow()
    one_day_ago = now - datetime.timedelta(hours=24)
    
    session_id = None
    
    if db:
        try:
            doc_ref = db.collection("active_sessions").document(phone)
            doc = doc_ref.get()
            
            if doc.exists:
                data = doc.to_dict()
                updated_at_str = data.get("updatedAt")
                if updated_at_str:
                    updated_at = datetime.datetime.fromisoformat(updated_at_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    if updated_at > one_day_ago:
                        session_id = data.get("active_session_id")
                        print(f"[SESSION] Reutilizando sesión activa de Firestore: {session_id} para {phone}")
            
            if not session_id:
                session_id = str(uuid.uuid4())
                # Crear mapeo de sesión activa
                doc_ref.set({
                    "active_session_id": session_id,
                    "updatedAt": now.isoformat()
                })
                # Inicializar documento de la sesión
                db.collection("sessions").document(session_id).set({
                    "id": session_id,
                    "phone": phone,
                    "status": "active",
                    "createdAt": now.isoformat(),
                    "lastInteraction": now.isoformat()
                })
                print(f"[SESSION] Nueva sesión creada en Firestore: {session_id} para {phone}")
                
        except Exception as e:
            print(f"[SESSION ERROR] Error interactuando con Firestore para sesión: {e}. Usando fallback local.")
            db = None # Forzar fallback local en catch
            
    if not db:
        # Fallback local mock
        active_sess = MOCK_ACTIVE_SESSIONS.get(phone)
        if active_sess:
            updated_at = active_sess["updatedAt"]
            if updated_at > one_day_ago:
                session_id = active_sess["active_session_id"]
                print(f"[SESSION] Reutilizando sesión activa local: {session_id} para {phone}")
        
        if not session_id:
            session_id = str(uuid.uuid4())
            MOCK_ACTIVE_SESSIONS[phone] = {
                "active_session_id": session_id,
                "updatedAt": now
            }
            print(f"[SESSION] Nueva sesión local creada: {session_id} para {phone}")
            
    return session_id

def update_session_timestamp(phone: str, session_id: str):
    """Actualiza la marca de tiempo de la última interacción de la sesión en Firestore o local."""
    db = get_firestore_client()
    now = datetime.datetime.utcnow()
    
    if db:
        try:
            db.collection("active_sessions").document(phone).update({
                "updatedAt": now.isoformat()
            })
            db.collection("sessions").document(session_id).update({
                "lastInteraction": now.isoformat()
            })
        except Exception as e:
            print(f"[SESSION ERROR] No se pudo actualizar timestamp en Firestore: {e}")
    else:
        if phone in MOCK_ACTIVE_SESSIONS:
            MOCK_ACTIVE_SESSIONS[phone]["updatedAt"] = now

def send_outbound_message(phone: str, message: str):
    """Llama de forma síncrona a la API de salida de Codio para responder por WhatsApp."""
    url = f"{CODIO_API_URL}/api/v1/messages/send"
    payload = {
        "to": phone,
        "type": "text",
        "body": message
    }
    
    print(f"[OUTBOUND] Enviando respuesta a Codio para: {phone}...")
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"[OUTBOUND] Mensaje enviado exitosamente a través de Codio. Respuesta: {response.json()}")
        else:
            print(f"[OUTBOUND ERROR] La API de Codio respondió con código {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[OUTBOUND ERROR] No se pudo conectar a la API de salida de Codio: {e}")

async def process_agent_interaction(phone: str, message_body: str, session_id: str):
    """Maneja el procesamiento de la conversación con el ADK Runner en background."""
    try:
        # 1. Asegurar que la sesión existe en el InMemorySessionService de ADK
        try:
            await session_service.get_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
        except Exception:
            # Si no existe en el servicio en memoria de ADK (ej: por reinicio), la creamos
            await session_service.create_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
        
        # 2. Inyectar variables críticas del contexto en el estado de sesión para las herramientas
        session = await session_service.get_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
        session.state["phone"] = phone
        session.state["session_id"] = session_id
        
        # 3. Ejecutar el agente ADK de forma asíncrona
        response_text = ""
        print(f"[ADK] Ejecutando agente para {phone} en sesión {session_id}...")
        
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
            update_session_timestamp(phone, session_id)
            
        # 5. Despachar el mensaje de salida de vuelta al usuario final a través de Codio
        if response_text:
            send_outbound_message(phone, response_text)
        else:
            print("[ADK WARNING] El agente no generó respuesta de texto final.")
            
    except Exception as e:
        print(f"[PROCESS ERROR] Error crítico procesando interacción de IA para {phone}: {e}")
        traceback.print_exc()
        # Enviar mensaje de error fallback para evitar que el chat quede colgado
        send_outbound_message(
            phone, 
            "Disculpas, estoy experimentando dificultades técnicas temporales. Por favor, reintenta tu consulta en unos momentos. 🩺"
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
        
        if not phone or not body_text:
            print("[WEBHOOK ERROR] El payload no contiene campos 'phone' o 'body' válidos.")
            raise HTTPException(status_code=400, detail="Missing phone or body fields")
            
        # 1. Resolver o crear el UUID de sesión para este número de teléfono (con TTL de 24hs)
        session_id = resolve_active_session(phone)
        
        # 2. Despachar el procesamiento pesado del Agente ADK a una tarea de fondo (asíncrona)
        # Esto permite responderle 200 OK inmediatamente al webhook de Codio
        background_tasks.add_task(process_agent_interaction, phone, body_text, session_id)
        
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
