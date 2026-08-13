import os
import datetime
import traceback
import requests
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
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

def send_outbound_message(phone: str, message: str, account_sid: str = "AC_default", session_id: str = None):
    """Llama de forma síncrona a la API de salida de Codio para responder por WhatsApp o Web."""
    url = f"{CODIO_API_URL}/api/v1/messages/send"
    payload = {
        "to": phone,
        "type": "text",
        "body": message,
        "accountSid": account_sid
    }
    if session_id:
        payload["sessionId"] = session_id
    
    print(f"[OUTBOUND] Enviando respuesta a Codio para: {phone} (Cuenta: {account_sid}, Sesión: {session_id})...")
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
            try:
                await session_service.delete_session(app_name="cadio-agente", user_id=phone, session_id=session_id)
                MOCK_ACTIVE_SESSIONS.pop(phone, None)
            except Exception as e:
                print(f"[SESSION ERROR] Error borrando sesión de ADK: {e}")
            
        # 5. Despachar el mensaje de salida de vuelta al usuario final a través de Codio
        if response_text:
            send_outbound_message(phone, response_text, account_sid, session_id)
        else:
            print("[ADK WARNING] El agente no generó respuesta de texto final.")
            
    except Exception as e:
        print(f"[PROCESS ERROR] Error crítico procesando interacción de IA para {phone}: {e}")
        traceback.print_exc()
        send_outbound_message(
            phone, 
            "Disculpas, estoy experimentando dificultades técnicas temporales. Por favor, reintenta tu consulta en unos momentos. 🩺",
            account_sid,
            session_id
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
        
        # Interceptar tipos multimedia para evitar pasar HTML plano al agente Gemini ADK
        msg_type = data.get("type", "text")
        if msg_type == "image":
            body_text = "[El usuario ha enviado una imagen/foto]"
        elif msg_type == "video":
            body_text = "[El usuario ha enviado un video]"
        elif msg_type == "document":
            body_text = "[El usuario ha enviado un documento/archivo]"

        if not phone or not body_text or not session_id:
            print("[WEBHOOK ERROR] El payload no contiene campos requeridos ('phone', 'body', 'sessionId').")
            raise HTTPException(status_code=400, detail="Missing required payload fields")
            
        # Despachar el procesamiento pesado del Agente ADK a una tarea de fondo (asíncrona)
        background_tasks.add_task(process_agent_interaction, phone, body_text, session_id, account_sid)
        
        return {"status": "accepted", "session_id": session_id}
        
    except Exception as e:
        print(f"[WEBHOOK ERROR] Error procesando solicitud: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/chat")
async def direct_chat(request: Request):
    """Endpoint directo para pruebas desde Postman que crea la sesión de forma dinámica si no existe."""
    try:
        payload = await request.json()
        user_id = payload.get("user_id")
        session_id = payload.get("session_id")
        message = payload.get("message")
        account_sid = payload.get("account_sid", "AC_default")

        if not user_id or not session_id or not message:
            raise HTTPException(status_code=400, detail="Faltan campos requeridos: user_id, session_id, message")

        # 1. Asegurar que la sesión existe en el InMemorySessionService de ADK
        session = await session_service.get_session(app_name="cadio-agente", user_id=user_id, session_id=session_id)
        if session is None:
            await session_service.create_session(app_name="cadio-agente", user_id=user_id, session_id=session_id)
            session = await session_service.get_session(app_name="cadio-agente", user_id=user_id, session_id=session_id)

        # 2. Inyectar variables críticas del contexto en el estado de sesión para las herramientas
        session.state["phone"] = user_id
        session.state["session_id"] = session_id
        session.state["account_sid"] = account_sid

        # 3. Ejecutar el agente ADK de forma asíncrona
        response_text = ""
        print(f"[DIRECT CHAT] Ejecutando agente para {user_id} en sesión {session_id}...")
        
        new_msg = types.Content(
            role="user",
            parts=[types.Part.from_text(text=message)]
        )
        
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_msg
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    response_text = event.content.parts[0].text

        # 4. Verificar si la herramienta end_conversation marcó la sesión como cerrada
        is_closed = session.state.get("session_closed", False)
        if is_closed:
            print(f"[DIRECT CHAT] La sesión {session_id} ha sido cerrada explícitamente.")
            try:
                await session_service.delete_session(app_name="cadio-agente", user_id=user_id, session_id=session_id)
                MOCK_ACTIVE_SESSIONS.pop(user_id, None)
            except Exception as e:
                print(f"[SESSION ERROR] Error borrando sesión de ADK: {e}")

        return {
            "success": True,
            "user_id": user_id,
            "session_id": session_id,
            "response": response_text,
            "session_closed": is_closed
        }

    except Exception as e:
        print(f"[DIRECT CHAT ERROR] Error procesando interacción directa: {e}")
        traceback.print_exc()
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
