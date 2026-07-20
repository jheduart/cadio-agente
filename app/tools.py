import os
import uuid
import datetime
from typing import Dict, Any, List
from google.adk.tools import ToolContext

# Almacenamiento local simulado para cuando corremos sin credenciales GCP/Firestore reales
MOCK_APPOINTMENTS: Dict[str, List[Dict[str, Any]]] = {}
MOCK_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}

def get_firestore_client():
    """Inicializa y retorna el cliente de Firestore si las credenciales están disponibles."""
    try:
        from google.cloud import firestore
        project_id = os.environ.get("PROJECT_ID", "proveedores-dev")
        # Si no hay credenciales locales, firestore.Client() lanzará una excepción
        return firestore.Client(project=project_id)
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

async def end_conversation(summary_of_session: str, tool_context: ToolContext) -> Dict[str, Any]:
    """Finaliza de forma explícita la conversación actual, registrando un resumen en Firestore y limpiando el estado de sesión activa.

    Args:
        summary_of_session: Un resumen conciso de los temas tratados en el chat, incluyendo turnos agendados o consultas resueltas. Debe omitir la verificación inicial de identidad del usuario.

    Returns:
        dict indicando que la sesión ha sido cerrada exitosamente.
    """
    # Obtener el ID de la sesión y el número de teléfono del estado del ToolContext de ADK
    session_id = tool_context.state.get("session_id")
    phone = tool_context.state.get("phone")

    print(f"[LIFECYCLE] Ejecutando end_conversation para sesión {session_id} | Teléfono: {phone}")

    # Indicar a la aplicación que limpie este mapeo de sesión activa para que el próximo saludo abra una nueva sesión
    tool_context.state["session_closed"] = True

    db = get_firestore_client()
    session_data = {
        "id": session_id,
        "phone": phone,
        "status": "closed",
        "summary": summary_of_session,
        "closedAt": datetime.datetime.utcnow().isoformat(),
        "lastInteraction": datetime.datetime.utcnow().isoformat()
    }

    if db:
        try:
            # 1. Actualizar el estado y resumen en la colección de sesiones
            db.collection("sessions").document(session_id).set(session_data, merge=True)
            # 2. Borrar de forma segura la sesión activa para este teléfono para forzar nuevo UUID en el re-saludo
            db.collection("active_sessions").document(phone).delete()
            print(f"[LIFECYCLE] Sesión {session_id} marcada como cerrada en Firestore con éxito.")
        except Exception as e:
            print(f"[LIFECYCLE ERROR] Error interactuando con Firestore en end_conversation: {e}")
            # Fallback local
            MOCK_ACTIVE_SESSIONS.pop(phone, None)
    else:
        # Fallback local
        MOCK_ACTIVE_SESSIONS.pop(phone, None)

    return {
        "status": "success",
        "message": "Conversación finalizada exitosamente.",
        "session_id": session_id,
        "summary_recorded": True
    }
