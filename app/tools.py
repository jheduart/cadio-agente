import os
import uuid
import datetime
import traceback
from typing import Dict, Any, List
from google.adk.tools import ToolContext

# Almacenamiento local simulado para cuando corremos sin credenciales GCP/Firestore reales
MOCK_APPOINTMENTS: Dict[str, List[Dict[str, Any]]] = {}
MOCK_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}

_db_client = None

def get_firestore_client():
    """Inicializa y retorna el cliente de Firestore reutilizando el singleton global para evitar latencias de autenticación."""
    global _db_client
    if _db_client is not None:
        return _db_client

    try:
        from google.cloud import firestore
        project_id = os.environ.get("PROJECT_ID", "proveedores-dev")
        _db_client = firestore.Client(project=project_id)
        print(f"[FIRESTORE] Cliente Singleton inicializado para proyecto '{project_id}'")
        return _db_client
    except Exception as e:
        print(f"[FIRESTORE WARNING] Corriendo en modo local simulado (no se pudo conectar a Firestore): {e}")
        return None

def list_specialties() -> Dict[str, Any]:
    """Retorna la lista de especialidades médicas disponibles en la clínica Cadio Salud.

    Returns:
        dict con la clave 'specialties' que contiene la lista de especialidades.
    """
    return {
        "status": "success",
        "specialties": [
            "Cardiología",
            "Pediatría",
            "Medicina General",
            "Dermatología"
        ]
    }

def list_doctors(specialty: str) -> Dict[str, Any]:
    """Retorna la lista de médicos disponibles para una especialidad dada y sus horarios de atención.

    Args:
        specialty: El nombre de la especialidad médica (ej. Cardiología).

    Returns:
        dict con el estado de la búsqueda y la lista de médicos con sus horarios.
    """
    spec_clean = specialty.strip().capitalize()
    
    doctors_db = {
        "Cardiología": [
            {"name": "Dr. Andrés Gómez", "days": "Lunes, Miércoles y Viernes", "hours": "09:00 - 13:00"}
        ],
        "Pediatría": [
            {"name": "Dra. Mariana Sosa", "days": "Martes y Jueves", "hours": "14:00 - 18:00"}
        ],
        "Medicina General": [
            {"name": "Dr. Carlos Pérez", "days": "Lunes a Viernes", "hours": "08:00 - 12:00 y 15:00 - 19:00"}
        ],
        "Dermatología": [
            {"name": "Dra. Sofía Rossi", "days": "Miércoles", "hours": "10:00 - 16:00"}
        ]
    }

    if spec_clean in doctors_db:
        return {
            "status": "success",
            "specialty": spec_clean,
            "doctors": doctors_db[spec_clean]
        }
    else:
        # Intenta buscar coincidencia parcial
        for spec, docs in doctors_db.items():
            if spec_clean in spec or spec in spec_clean:
                return {
                    "status": "success",
                    "specialty": spec,
                    "doctors": docs
                }
        return {
            "status": "error",
            "message": f"No se encontraron médicos para la especialidad '{specialty}'."
        }

def book_appointment(patient_phone: str, doctor_name: str, date: str, time: str) -> Dict[str, Any]:
    """Registra una cita médica con un doctor en una fecha y hora específicas.

    Args:
        patient_phone: El número de teléfono del paciente que reserva el turno.
        doctor_name: El nombre del médico con quien se reserva el turno.
        date: La fecha del turno en formato DD/MM/AAAA.
        time: La hora del turno en formato HH:MM (ej. 10:30).

    Returns:
        dict con el estado de la reserva, ID de confirmación y detalles del turno.
    """
    db = get_firestore_client()
    appointment_id = f"appt-{uuid.uuid4().hex[:8]}"
    
    appointment_data = {
        "appointment_id": appointment_id,
        "patient_phone": patient_phone,
        "doctor_name": doctor_name,
        "date": date,
        "time": time,
        "status": "confirmed",
        "createdAt": datetime.datetime.utcnow().isoformat()
    }

    if db:
        try:
            # Guardar en la colección global 'appointments'
            db.collection("appointments").document(appointment_id).set(appointment_data)
            print(f"[FIRESTORE] Cita médica {appointment_id} registrada con éxito.")
        except Exception as e:
            print(f"[FIRESTORE ERROR] Error al guardar cita en Firestore: {e}")
            # Fallback a almacenamiento local si falla Firestore
            if patient_phone not in MOCK_APPOINTMENTS:
                MOCK_APPOINTMENTS[patient_phone] = []
            MOCK_APPOINTMENTS[patient_phone].append(appointment_data)
    else:
        # Guardar en local mock
        if patient_phone not in MOCK_APPOINTMENTS:
            MOCK_APPOINTMENTS[patient_phone] = []
        MOCK_APPOINTMENTS[patient_phone].append(appointment_data)

    return {
        "status": "success",
        "appointment_id": appointment_id,
        "message": "Turno confirmado con éxito",
        "details": appointment_data
    }

def get_my_appointments(patient_phone: str) -> Dict[str, Any]:
    """Consulta la lista de turnos médicos confirmados asociados a un número de teléfono.

    Args:
        patient_phone: El número de teléfono del paciente para realizar la búsqueda.

    Returns:
        dict con la lista de citas médicas confirmadas asociadas al número de teléfono.
    """
    db = get_firestore_client()
    appointments = []

    if db:
        try:
            docs = db.collection("appointments").where("patient_phone", "==", patient_phone).where("status", "==", "confirmed").stream()
            for doc in docs:
                appointments.append(doc.to_dict())
            print(f"[FIRESTORE] Se encontraron {len(appointments)} citas para el teléfono: {patient_phone}")
        except Exception as e:
            print(f"[FIRESTORE ERROR] Error consultando citas en Firestore: {e}")
            # Fallback a local mock
            appointments = MOCK_APPOINTMENTS.get(patient_phone, [])
    else:
        appointments = MOCK_APPOINTMENTS.get(patient_phone, [])

    return {
        "status": "success",
        "patient_phone": patient_phone,
        "appointments": appointments
    }

def end_conversation(summary_of_session: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Finaliza de forma explícita la conversación actual, registrando un resumen en Firestore y limpiando el estado de sesión activa.

    Args:
        summary_of_session: Un resumen conciso de los temas tratados en el chat, incluyendo turnos agendados o consultas resueltas. Debe omitir la verificación inicial de identidad del usuario.

    Returns:
        dict indicando que la sesión ha sido cerrada exitosamente.
    """
    # Obtener el ID de la sesión y el número de teléfono
    session_id = None
    phone = None
    account_sid = "AC_default"
    
    if hasattr(tool_context, "session") and tool_context.session:
        session_id = tool_context.session.id
        phone = tool_context.session.user_id
        if hasattr(tool_context.session, "state") and tool_context.session.state:
            account_sid = tool_context.session.state.get("account_sid", "AC_default")
    if not session_id and tool_context.state:
        session_id = tool_context.state.get("session_id")
    if not phone and tool_context.state:
        phone = tool_context.state.get("phone")
    if account_sid == "AC_default" and tool_context.state:
        account_sid = tool_context.state.get("account_sid", "AC_default")

    print(f"[LIFECYCLE] Ejecutando end_conversation para sesión {session_id} | Teléfono: {phone}")

    tool_context.state["session_closed"] = True

    db = get_firestore_client()

    if db:
        try:
            # Obtener el canal activo antes de cerrar la sesión
            session_ref = db.collection("cuentas").document(account_sid).collection("sesiones").document(session_id)
            session_snap = session_ref.get()
            session_data = session_snap.to_dict() if session_snap.exists else {}
            canal_activo = session_data.get("canalActivo", "whatsapp")

            # 1. Actualizar el estado y resumen en la colección SaaS de sesiones
            session_ref.update({
                "estado": "cerrada",
                "cerradaAt": datetime.datetime.utcnow(),
                "resumenBot": summary_of_session
            })
            # 2. Desvincular el session id del contacto para forzar nuevo UUID en el re-saludo
            db.collection("cuentas").document(account_sid).collection("contactos").document(phone).update({
                "sesionActivaId": None
            })
            print(f"[LIFECYCLE] Sesión SaaS {session_id} marcada como cerrada en Firestore con éxito.")

            # 3. Notificación forzada por WhatsApp de citas médicas importantes
            if canal_activo == "web" and summary_of_session:
                summary_lower = summary_of_session.lower()
                is_appointment = any(kw in summary_lower for kw in ["cita", "turno", "agend", "reserva", "dermatólog", "médic"])
                if is_appointment:
                    print(f"[LIFECYCLE] Detectada cita médica en Chat Web al cerrar. Despachando resumen informativo a WhatsApp de forma forzada.")
                    import requests
                    CODIO_API_URL = os.environ.get("CODIO_API_URL", "http://localhost:8080")
                    payload = {
                        "to": phone,
                        "type": "text",
                        "body": f"🩺 *Resumen de tu Cita Médica en Cadio Salud* 🩺\n\nHemos registrado la siguiente información importante sobre tu consulta web:\n\n{summary_of_session}\n\n¡Gracias por confiar en nosotros!",
                        "accountSid": account_sid,
                        "sessionId": session_id,
                        "forceChannel": "whatsapp"
                    }
                    try:
                        resp = requests.post(f"{CODIO_API_URL}/api/v1/messages/send", json=payload, headers={"x-account-sid": account_sid}, timeout=10)
                        print(f"[LIFECYCLE] Despacho de resumen por WhatsApp completado con estado: {resp.status_code}")
                    except Exception as req_err:
                        print(f"[LIFECYCLE ERROR] No se pudo enviar el resumen por WhatsApp: {req_err}")
        except Exception as e:
            print(f"[LIFECYCLE ERROR] Error interactuando con Firestore SaaS en end_conversation: {e}")
            traceback.print_exc()
            MOCK_ACTIVE_SESSIONS.pop(phone, None)
    else:
        MOCK_ACTIVE_SESSIONS.pop(phone, None)

    return {
        "status": "success",
        "message": "Conversación finalizada exitosamente.",
        "session_id": session_id,
        "summary_recorded": True
    }

def derivar_a_asesor(motivo: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Deriva la conversación actual a un asesor humano cuando el usuario lo solicite explícitamente, o cuando sus preguntas excedan el alcance de la clínica.

    Args:
        motivo: Breve explicación del por qué se deriva el chat (ej. 'El usuario solicita hablar con un asesor', 'Pregunta sobre costos médicos complejos').

    Returns:
        dict indicando que la derivación ha sido registrada exitosamente.
    """
    session_id = None
    phone = None
    account_sid = "AC_default"
    
    if hasattr(tool_context, "session") and tool_context.session:
        session_id = tool_context.session.id
        phone = tool_context.session.user_id
        if hasattr(tool_context.session, "state") and tool_context.session.state:
            account_sid = tool_context.session.state.get("account_sid", "AC_default")
    if not session_id and tool_context.state:
        session_id = tool_context.state.get("session_id")
    if not phone and tool_context.state:
        phone = tool_context.state.get("phone")
    if account_sid == "AC_default" and tool_context.state:
        account_sid = tool_context.state.get("account_sid", "AC_default")

    equipo_id = "equipo_soporte_default"
    if tool_context.state and tool_context.state.get("equipo_id"):
        equipo_id = tool_context.state.get("equipo_id")

    print(f"[HANDOFF] Ejecutando derivar_a_asesor para sesión {session_id} | Teléfono: {phone} | Cuenta: {account_sid} | Equipo: {equipo_id} | Motivo: {motivo}")

    tool_context.state["session_closed"] = True

    db = get_firestore_client()
    
    if db:
        try:
            # 1. Actualizar de forma atómica el estado de atención de la sesión en Firestore usando set + merge
            sesion_ref = db.collection("cuentas").document(account_sid).collection("sesiones").document(session_id)
            
            sesion_ref.set({
                "estado": "abierta",
                "atencion": {
                    "modo": "esperando_asesor",
                    "motivo_derivacion": motivo,
                    "equipo_id": equipo_id,
                    "asesor_id": None,
                    "solicitado_at": datetime.datetime.utcnow()
                }
            }, merge=True)
            
            print(f"[HANDOFF] Sesión {session_id} marcada exitosamente en Firestore como 'esperando_asesor'")

            # 2. Llamar al endpoint de la API del Panel para disparar el ruteo PUSH automático
            import requests
            panel_api_url = os.environ.get("PANEL_API_URL", "https://codio-panel-api-dev-ekczug53zq-uc.a.run.app")
            try:
                deriv_resp = requests.post(
                    f"{panel_api_url}/api/sesiones/{session_id}/derivar",
                    json={"equipo_id": equipo_id, "motivo": motivo},
                    headers={
                        "Authorization": "Bearer mock-token-bot",
                        "X-Cron-Secret": "codio-cron-secret-dev",
                        "x-account-sid": account_sid,
                        "x-cuenta-id": account_sid,
                        "Content-Type": "application/json"
                    },
                    timeout=5
                )
                print(f"[HANDOFF] Notificación enviada a Panel API ({deriv_resp.status_code}): {deriv_resp.text}")
            except Exception as req_err:
                print(f"[HANDOFF WARNING] No se pudo notificar a Panel API para ruteo PUSH: {req_err}")

        except Exception as e:
            print(f"[HANDOFF ERROR] Error al guardar derivación en Firestore: {e}")
            import traceback
            traceback.print_exc()
    else:
        print("[HANDOFF WARNING] Sin conexión a Firestore. Ignorando guardado físico.")

    return {
        "status": "success",
        "message": "Conversación derivada a un asesor humano de forma exitosa.",
        "session_id": session_id,
        "equipo_id": equipo_id,
        "handoff_triggered": True
    }
