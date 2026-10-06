"""Authenticated, deterministic banking demo workflows; no financial transactions.

The intent model classifies language. It cannot grant permissions or run tools.
All private data and state changes pass code-enforced rules below.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .policy import permitted


def plain(text):
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold())
                   if unicodedata.category(c) != "Mn").strip()


def utc_now():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


MESSAGES = {
    "too_long": ("Por favor, acorta tu consulta a un máximo de 2.000 caracteres para que pueda interpretarla completa.",
                 "Por favor, reduza sua consulta a no máximo 2.000 caracteres para que eu possa interpretá-la por completo."),
    "multiple_requests": ("Veo varias consultas en tu mensaje. ¿Cuál atendemos primero: cuentas, actividad o condiciones de la campaña?",
                          "Vejo várias consultas em sua mensagem. Qual atendemos primeiro: contas, atividade ou condições da campanha?"),
    "catalog_unavailable": ("No encontré información de la campaña solicitada en el catálogo disponible. ¿Puedes aclarar qué campaña necesitas o prefieres solicitar un asesor?",
                            "Não encontrei informações sobre a campanha solicitada no catálogo disponível. Você pode esclarecer qual campanha precisa ou prefere solicitar um assessor?"),
    "source_changed": ("La información disponible cambió desde que preparé esta acción. Revisa los datos actualizados y solicita la acción de nuevo antes de confirmarla.",
                       "As informações disponíveis mudaram desde que preparei esta ação. Confira os dados atualizados e solicite a ação novamente antes de confirmar."),
    "action_still_pending": ("La acción sigue pendiente y no se realizó ningún cambio. ¿Quieres confirmarla o dejarla pendiente?",
                             "A ação continua pendente e nenhuma alteração foi feita. Você quer confirmar ou deixá-la pendente?"),
    "denied": ("No puedo acceder a esa información con esta sesión. Inicia sesión con tu usuario autorizado.",
               "Não posso acessar essas informações com esta sessão. Entre com seu usuário autorizado."),
    "guard": ("Solo puedo consultar información autorizada para tu sesión y aplicar sus permisos.",
              "Só posso consultar informações autorizadas para sua sessão e aplicar suas permissões."),
    "clarify": ("¿Quieres información sobre una campaña, tus cuentas, tu actividad o hablar con un asesor?",
                "Você quer informações sobre uma campanha, suas contas, sua atividade ou falar com um assessor?"),
    "terms": ("El catálogo no contiene tasas, comisiones ni condiciones comerciales aprobadas para esta campaña. Puedo registrar una solicitud para que un asesor las aclare. ¿Confirmas la solicitud?",
              "O catálogo não contém taxas, tarifas nem condições comerciais aprovadas para esta campanha. Posso registrar uma solicitação para um assessor esclarecer. Você confirma a solicitação?"),
    "handoff": ("Puedo registrar una solicitud de atención en la cola local de Colombia/Ahorro con el contexto de esta conversación. ¿Confirmas la solicitud?",
                "Posso registrar uma solicitação de atendimento na fila local Colômbia/Poupança com o contexto desta conversa. Você confirma a solicitação?"),
    "created": ("La solicitud quedó registrada y comprobada en la cola local. Su estado es pendiente de atención.",
                "A solicitação foi registrada e conferida na fila local. Seu estado é pendente de atendimento."),
    "optout": ("Puedo guardar tu preferencia de no recibir publicidad. La atención que tú solicites seguirá disponible. ¿Confirmas el cambio?",
               "Posso salvar sua preferência de não receber publicidade. O atendimento que você solicitar continuará disponível. Você confirma a alteração?"),
    "optout_done": ("Tu preferencia de no recibir publicidad quedó guardada y comprobada.",
                    "Sua preferência de não receber publicidade foi salva e conferida."),
    "cancel": ("La acción pendiente se canceló. No se realizó ningún cambio.",
               "A ação pendente foi cancelada. Nenhuma alteração foi feita."),
    "tool_error": ("No pude comprobar el resultado de la operación. No la doy por completada. Puedes reintentar la acción pendiente.",
                   "Não consegui conferir o resultado da operação. Não a considero concluída. Você pode tentar novamente a ação pendente."),
    "classifier_error": ("No pude interpretar tu consulta en este momento. Intenta enviarla de nuevo.",
                         "Não consegui interpretar sua consulta neste momento. Tente enviá-la novamente."),
    "unsupported": ("Puedo atender consultas de Cuenta de Ahorro o registrar una solicitud de asesor. Las evaluaciones de crédito requieren otro proceso.",
                    "Posso atender consultas sobre conta poupança ou registrar uma solicitação de assessor. Avaliações de crédito exigem outro processo."),
    "unavailable": ("Las fechas del perfil no permiten una consulta coherente al corte de datos. Puedo explicar el catálogo o registrar una solicitud de aclaración.",
                    "As datas do perfil não permitem uma consulta coerente no corte dos dados. Posso explicar o catálogo ou registrar uma solicitação de esclarecimento."),
    "greeting": ("Hola. Puedo ayudarte con campañas de ahorro, tus cuentas, actividad observada y solicitudes de asesor.",
                 "Olá. Posso ajudar com campanhas de poupança, suas contas, atividade observada e solicitações de assessor."),
}


class IntentRouter:
    """Select baseline, learned or hybrid routing with the configured model."""
    def __init__(self, model, mode="hybrid"):
        if mode not in ("baseline", "learned", "hybrid"):
            raise ValueError("Unknown router mode")
        self.model, self.mode = model, mode

    def predict(self, text):
        from .intents import baseline_predict, hybrid_predict
        if self.mode == "baseline":
            return {**baseline_predict(text), "routing_source": "baseline", "model_provider": "baseline"}
        if self.mode == "learned":
            result = {**self.model.predict(text), "routing_source": "learned"}
        else:
            result = hybrid_predict(text, self.model)
        return {**result, "model_provider": getattr(self.model, "provider", "tfidf"),
                "model_version": getattr(self.model, "model_version", "tfidf-softmax-es-pt-v1")}


class ChatService:
    """Thread-safe local service with SQLite-backed sessions, conversations and tools.

    ``fault_injector(stage)`` is trusted evaluation configuration, never user input.
    Test sessions are only available through Python; the HTTP server never exposes
    issuance, user creation, role changes or arbitrary customer selection.
    """

    def __init__(self, store, model, state_path, fault_injector=None):
        self.store, self.model = store, model
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.fault_injector = fault_injector
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users(
                    username TEXT PRIMARY KEY, salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    principal TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, owner TEXT NOT NULL, principal TEXT NOT NULL,
                    expires_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS conversations(
                    conversation_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    customer_id TEXT, language TEXT NOT NULL, history TEXT NOT NULL,
                    pending TEXT, last_action TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests(
                    request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, customer_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, status TEXT NOT NULL, queue TEXT NOT NULL,
                    language TEXT NOT NULL, context TEXT NOT NULL, source_version TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(owner,idempotency_key));
                CREATE TABLE IF NOT EXISTS consent_overrides(
                    customer_id TEXT PRIMARY KEY, accepts_marketing INTEGER NOT NULL,
                    source TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                    audit_id INTEGER PRIMARY KEY, owner TEXT NOT NULL, action TEXT NOT NULL,
                    outcome TEXT NOT NULL, reference TEXT, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.state_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _fault(self, stage):
        if self.fault_injector:
            self.fault_injector(stage)

    def _msg(self, key, language):
        return MESSAGES[key][1 if language == "pt" else 0]

    def _result(self, status, message, language, intent="unknown", **extra):
        return dict(status=status, outcome=status, message=message, language=language,
                    intent=intent, facts={}, evidence=[], pending_action=None,
                    request_id=None, tool_events=[], **extra)

    def _response(self, status, key, language, intent="unknown", **extra):
        result = self._result(status, self._msg(key, language), language, intent)
        result.update(extra)
        return result

    def register_demo_user(self, username, password, customer_id=None, role="customer",
                           assigned_customer_ids=()):
        """Trusted local bootstrap only. Existing credentials are never overwritten."""
        if role not in ("customer", "advisor", "operator") or not username or len(password) < 12:
            raise ValueError("Invalid demo credentials or role")
        if role == "customer" and not customer_id:
            raise ValueError("Customer account requires trusted customer mapping")
        principal = dict(authenticated=True, expired=False, role=role,
                         customer_id=customer_id, assigned_customer_ids=list(assigned_customer_ids))
        salt = secrets.token_hex(24)
        hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240_000).hex()
        with self._lock, self._connect() as conn:
            previous = conn.execute("SELECT principal FROM users WHERE username=?", (username,)).fetchone()
            if previous:
                if json.loads(previous[0]) != principal:
                    raise ValueError("Existing trusted user mapping differs")
                return False
            conn.execute("INSERT INTO users(username,salt,password_hash,principal) VALUES(?,?,?,?)",
                         (username, salt, hashed, canonical(principal)))
        return True

    def login(self, username, password):
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 100 or len(password) > 300:
            return None
        with self._lock, self._connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE username=? AND enabled=1", (username,)).fetchone()
            salt = user["salt"] if user else "00" * 24
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 240_000).hex()
            expected = user["password_hash"] if user else "00" * 32
            if not hmac.compare_digest(actual, expected) or user is None:
                return None
            return self._issue_session(conn, user["username"], json.loads(user["principal"]))

    def _issue_session(self, conn, owner, principal, expired=False):
        token = secrets.token_urlsafe(40)
        expires = utc_now() + timedelta(minutes=-1 if expired else 30)
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,0)",
                     (hashlib.sha256(token.encode()).hexdigest(), owner, canonical(principal), expires.isoformat()))
        return dict(token=token, expires_at=expires.isoformat(), role=principal["role"])

    def issue_test_session(self, customer_id, role="customer", expired=False, assigned_customer_ids=()):
        principal = dict(authenticated=True, expired=False, role=role, customer_id=customer_id,
                         assigned_customer_ids=list(assigned_customer_ids))
        with self._lock, self._connect() as conn:
            return self._issue_session(conn, "fixture:" + role + ":" + str(customer_id), principal, expired)["token"]

    def _principal(self, conn, token):
        if not isinstance(token, str) or len(token) > 250:
            return None, None
        row = conn.execute("SELECT * FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if not row or row["revoked"] or datetime.fromisoformat(row["expires_at"]) <= utc_now():
            return None, None
        principal = json.loads(row["principal"])
        principal.update(authenticated=True, expired=False)
        return row["owner"], principal

    def logout(self, token):
        if isinstance(token, str):
            with self._lock, self._connect() as conn:
                conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def _allowed(self, principal, action, customer_id=None, confirmed=False):
        return permitted(principal or {}, action, customer_id,
                         (principal or {}).get("assigned_customer_ids", ()), confirmed)

    def _load_conversation(self, conn, conversation_id, owner, customer_id, language):
        if conversation_id:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=? AND owner=?", (conversation_id, owner)).fetchone()
            if row is None or row["customer_id"] != customer_id:
                return None
            return dict(row)
        conversation_id = "CONV-" + secrets.token_hex(12)
        now = utc_now().isoformat()
        conn.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
                     (conversation_id, owner, customer_id, language, "[]", None, None, now))
        return dict(conversation_id=conversation_id, owner=owner, customer_id=customer_id,
                    language=language, history="[]", pending=None, last_action=None, created_at=now)

    def _save_conversation(self, conn, conversation, message, response, pending=None, last_action=None):
        history = json.loads(conversation["history"])
        history += [dict(role="user", text=message),
                    dict(role="assistant", intent=response["intent"], status=response["status"],
                         facts=response.get("facts", {}), evidence=response.get("evidence", []),
                         actions=response.get("tool_events", []), text=response["message"])]
        conn.execute("UPDATE conversations SET history=?,language=?,pending=?,last_action=? WHERE conversation_id=?",
                     (canonical(history[-12:]), response["language"], canonical(pending) if pending else None,
                      canonical(last_action) if last_action else conversation.get("last_action"), conversation["conversation_id"]))
        response["conversation_id"] = conversation["conversation_id"]
        if "prediction" in conversation:
            response["prediction"] = conversation["prediction"]
            response["routing_source"] = conversation["prediction"].get("routing_source", "provided_predictor")
        if pending is not None:
            response["pending_action"] = pending

    def _guard(self, message, principal, customer_id):
        normalized = plain(message)
        injection = re.search(r"(ignora|ignore|desobedece|esqueca|olvida).{0,60}(reglas|regras|instrucciones|instructions|permissions|permisos)|"
                              r"(system prompt|prompt del sistema|sou administrador|soy administrador|actua como administrador|act as admin)|"
                              r"(muestra|mostrar|dame|liste|list|exibe|revela|reveal).{0,35}(todos los clientes|todos os clientes|all customers|contrasenas|senhas|passwords|tokens)", normalized)
        if injection:
            return "guard"
        ids = re.findall(r"\b(?:CUS|CUST|CLI|CLIENT)-[A-Z0-9]+\b", message, re.I)
        if any(i.upper() != str(customer_id).upper() for i in ids):
            return "denied"
        # Requests for another person's data cannot be approved by an intent model.
        if re.search(r"(otro cliente|otra persona|outro cliente|outra pessoa|other customer|other person)", normalized):
            return "denied"
        return None

    def _campaigns(self):
        keys = ("campaign_id", "campaign_name", "description", "promoted_product", "campaign_objective",
                "target_country", "target_segment", "channel", "start_date", "end_date", "campaign_status",
                "historical_replay_assumption", "quality_flags")
        return [{k: c[k] for k in keys if k in c} for c in self.store.list_campaigns()]

    @staticmethod
    def _multiple_requests(message):
        """Workflow scope check, separate from frozen intent-model predictions.

        A balance, observed activity and commercial terms require different
        evidence.  When the user joins multiple such requests, ask which to
        handle first rather than silently replacing them with one handoff.
        Generic references to an account/campaign do not count as another task.
        """
        text = plain(message)
        scopes = [bool(re.search(pattern, text)) for pattern in (
            r"\b(saldo\w*|balance)\b",
            r"\b(movimiento\w*|movimenta\w*|transac\w*|actividad|atividade|extracto\w*|extrato\w*)\b",
            r"\b(tasa\w*|taxa\w*|comision\w*|comisso\w*|tarifa\w*|condicion\w*|condico\w*|juros|intereses)\b")]
        return sum(scopes) > 1 and bool(re.search(r"\b(y|e|tambien|tambem|ambas|ambos|duas|dos)\b", text))

    def _context(self, conn, customer_id):
        self._fault("before_context_read")
        customer = self.store.get_customer(customer_id)
        if customer is None:
            return None
        override = conn.execute("SELECT accepts_marketing FROM consent_overrides WHERE customer_id=?", (customer_id,)).fetchone()
        return dict(customer=customer, accounts=self.store.get_accounts(customer_id),
                    activity=self.store.get_activity(customer_id), matches=self.store.campaign_matches(customer_id),
                    marketing_override=override[0] if override else None)

    def _pending(self, kind, customer_id, language, reason, conversation):
        return dict(action=kind, action_id="ACTION-" + secrets.token_hex(12), customer_id=customer_id,
                    idempotency_key=secrets.token_hex(16), language=language, reason=reason,
                    conversation_id=conversation["conversation_id"], source_version=str(self.store.source_version))

    def chat(self, token, message, conversation_id=None, language="es", target_customer_id=None,
             confirmed=False, idempotency_key=None):
        language = "pt" if language == "pt" else "es"
        if isinstance(message, str) and len(message) > 2000:
            return self._response("clarify", "too_long", language)
        if not isinstance(message, str) or not message.strip():
            return self._response("clarify", "clarify", language)
        if (conversation_id is not None and (not isinstance(conversation_id, str) or len(conversation_id) > 100)) or \
                (idempotency_key is not None and (not isinstance(idempotency_key, str) or len(idempotency_key) > 100)):
            return self._response("denied", "denied", language)
        with self._lock, self._connect() as conn:
            owner, principal = self._principal(conn, token)
            if not self._allowed(principal, "view_public_campaign"):
                return self._response("denied", "denied", language)
            customer_id = target_customer_id if target_customer_id is not None else principal.get("customer_id")
            if target_customer_id is not None and not self._allowed(principal, "view_customer_context", customer_id):
                return self._response("denied", "denied", language)
            guard = self._guard(message, principal, customer_id)
            if guard:
                return self._response("denied", guard, language)
            conversation = self._load_conversation(conn, conversation_id, owner, customer_id, language)
            if conversation is None:
                return self._response("denied", "denied", language)
            conn.commit()
            pending = json.loads(conversation["pending"]) if conversation.get("pending") else None
            normalized = plain(message)
            affirmative = normalized in ("si", "sim", "confirmo", "confirmar", "yes", "de acuerdo", "pode registrar", "puedes registrar", "acepto")
            negative = normalized in ("no", "nao", "cancelar", "cancela", "cancel", "no gracias", "nao obrigado")
            explicit_cancel = bool(re.search(r"\b(cancelar|cancela|cancele|cancel|cancelo|cancelamento)\b", normalized))
            cancellation_negated = bool(re.search(
                r"\b(no|nao)\s+(?:(quiero|deseo|quero|desejo)\s+(?:que\s+)?)?(cancelar|cancel\w*|cancele)\b", normalized))
            explicit_refusal = bool(re.search(r"\b(no registres|no registrar|no confirmo|nao registre|nao registrar|nao confirmo)\b", normalized))
            negative = negative or (explicit_cancel and not cancellation_negated) or explicit_refusal
            if pending and cancellation_negated:
                response = self._response("clarify", "action_still_pending", language, pending["action"])
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if pending and negative:
                response = self._response("resolution", "cancel", language, pending["action"])
                self._save_conversation(conn, conversation, message, response)
                return response
            if pending and (confirmed is True or affirmative):
                response = self._execute_pending(conn, principal, owner, conversation, pending, language, idempotency_key)
                if response["status"] == "tool_error":
                    response["pending_action"] = pending
                    self._save_conversation(conn, conversation, message, response, pending)
                else:
                    self._save_conversation(conn, conversation, message, response, last_action=pending)
                return response
            last_action = json.loads(conversation["last_action"]) if conversation.get("last_action") else None
            if not pending and confirmed is True and last_action and idempotency_key == last_action["idempotency_key"]:
                response = self._execute_pending(conn, principal, owner, conversation, last_action, language, idempotency_key)
                self._save_conversation(conn, conversation, message, response, last_action=last_action)
                return response
            if confirmed is True or affirmative:
                response = self._response("clarify", "clarify", language)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            from .clef import ClefError
            try:
                prediction = self.model.predict(message)
            except ClefError:
                response = self._response("tool_error", "classifier_error", language,
                                          routing_source="model_unavailable")
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            intent = prediction.get("intent", "unknown")
            history = json.loads(conversation["history"])
            if normalized in ("esa campana", "essa campanha", "y esa campana", "e essa campanha", "cuentame mas", "me conte mais"):
                last = next((h for h in reversed(history) if h.get("role") == "assistant" and h.get("intent") == "campaign_info"), None)
                if last:
                    intent = "campaign_info"
                    prediction = dict(intent=intent, confidence=1.0, ambiguous=False, routing_source="conversation_context")
            conversation["prediction"] = prediction
            if self._multiple_requests(message):
                response = self._response("clarify", "multiple_requests", language, intent,
                                          workflow_gate="multiple_requested_evidence_scopes")
                self._save_conversation(conn, conversation, message, response)
                return response
            if re.search(r"\b(campan\w*|promoc\w*|oferta\w*)\b", normalized) and not self._campaigns():
                response = self._response("clarify", "catalog_unavailable", language, intent,
                                          workflow_gate="requested_campaign_catalog_unavailable")
                self._save_conversation(conn, conversation, message, response)
                return response
            if prediction.get("ambiguous") or intent == "unknown":
                response = self._response("clarify", "clarify", language, intent)
                response["prediction"] = prediction
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if intent in ("greeting", "unsupported_credit"):
                response = self._response("resolution" if intent == "greeting" else "unsupported",
                                          "greeting" if intent == "greeting" else "unsupported", language, intent)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if intent in ("commercial_terms", "advisor_request", "marketing_optout"):
                action = "marketing_optout" if intent == "marketing_optout" else "request_advisor"
                if not self._allowed(principal, action, customer_id, confirmed=True):
                    response = self._response("denied", "denied", language, intent)
                    self._save_conversation(conn, conversation, message, response, pending)
                    return response
                kind = "marketing_optout" if intent == "marketing_optout" else "advisor_request"
                pending = self._pending(kind, customer_id, language, message, conversation)
                response = self._response("handoff_pending" if kind == "advisor_request" else "confirmation_pending",
                                          "optout" if kind == "marketing_optout" else "terms" if intent == "commercial_terms" else "handoff",
                                          language, intent, pending_action=pending)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if intent not in ("campaign_info", "account_info", "activity_info"):
                response = self._response("clarify", "clarify", language, intent)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if customer_id is None and intent == "campaign_info":
                response = self._public_campaign_response(language)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            if not self._allowed(principal, "view_customer_context", customer_id):
                response = self._response("denied", "denied", language, intent)
                self._save_conversation(conn, conversation, message, response, pending)
                return response
            try:
                context = self._context(conn, customer_id)
                if context is None or context["customer"].get("profile_available") is False:
                    response = self._response("clarify", "unavailable", language, intent)
                    response["evidence"] = [dict(source_version=str(self.store.source_version), type="profile_unavailable")]
                else:
                    response = self._informative_response(intent, language, context)
            except (sqlite3.Error, OSError, RuntimeError, ValueError, KeyError):
                response = self._response("tool_error", "tool_error", language, intent,
                                          tool_events=[dict(tool="read_context", outcome="failed")])
            self._save_conversation(conn, conversation, message, response, pending)
            return response

    def _public_campaign_response(self, language):
        campaigns = self._campaigns()
        message = ("El catálogo contiene campañas de Cuenta de Ahorro en Colombia correspondientes al corte de datos. Las condiciones comerciales no están disponibles."
                   if language == "es" else "O catálogo contém campanhas de conta poupança na Colômbia referentes ao corte dos dados. As condições comerciais não estão disponíveis.")
        response = self._result("resolution", message, language, "campaign_info")
        response.update(facts=dict(campaigns=campaigns), evidence=[dict(source_version=str(self.store.source_version), type="campaign_catalog")])
        return response

    def _informative_response(self, intent, language, context):
        evidence = [dict(source_version=str(self.store.source_version), type="historical_snapshot",
                         demo_at=self.store.config.get("demo_at"), historical_states_verified=False)]
        if intent == "account_info":
            allowed = ("product_id", "account_id", "product_type", "product_status", "status", "opening_date", "last_updated", "currency", "evidence", "historical_assumption")
            accounts = [{k: a[k] for k in allowed if k in a} for a in context["accounts"]]
            facts = dict(accounts=accounts, coherent_account_count=len(accounts), current_balance_available=False)
            message = ((f"Hay {len(accounts)} {'cuenta' if len(accounts) == 1 else 'cuentas'} con fechas coherentes al corte de datos. Los estados corresponden a la instantánea disponible; no hay saldo histórico verificado."
                        if accounts else "No encontré una cuenta con fechas coherentes al corte de datos; esto no demuestra que no tengas cuentas.") if language == "es" else
                       (f"Há {len(accounts)} {'conta' if len(accounts) == 1 else 'contas'} com datas coerentes no corte dos dados. Os estados correspondem à fotografia disponível; não há saldo histórico verificado."
                        if accounts else "Não encontrei uma conta com datas coerentes no corte dos dados; isso não prova que você não tenha contas."))
        elif intent == "activity_info":
            activity = context["activity"]
            facts = dict(activity=activity)
            count = activity.get("valid_known_transactions", activity.get("count", 0))
            recent = activity.get("recent_30d_count", 0)
            caveats = activity.get("quality_caveats", [])
            at = self.store.config.get("demo_at", "")[:10]
            message = (f"El historial contiene {count} movimientos válidos conocidos hasta {at}; {recent} están en los 30 días anteriores a ese corte. Muestro los últimos movimientos disponibles."
                       if language == "es" else f"O histórico contém {count} movimentos válidos conhecidos até {at}; {recent} estão nos 30 dias anteriores a esse corte. Mostro os últimos movimentos disponíveis.")
            if caveats:
                message += " Hay límites de calidad o cobertura en el historial." if language == "es" else " Há limites de qualidade ou cobertura no histórico."
        else:
            matches = context["matches"]
            opted_out = context["marketing_override"] == 0
            # Override never changes raw data or promotes an otherwise excluded customer.
            visible = []
            for match in matches:
                copy = dict(match)
                if opted_out:
                    copy["eligible"] = False
                    copy["reasons"] = list(copy.get("reasons", [])) + ["local_marketing_optout"]
                visible.append(copy)
            facts = dict(campaigns=self._campaigns(), selection=visible, marketing_optout=opted_out,
                         financial_eligibility_verified=False, guaranteed_benefit=False)
            eligible = any(m.get("eligible") for m in visible)
            if language == "es":
                message = ("Tu perfil coincide con los criterios de selección de una campaña de ahorro al corte de datos." if eligible else
                           "Tu perfil no está seleccionado para esta campaña según sus criterios; puedes consultar su información pública.")
                message += " La selección es por cuenta y no prueba inactividad total del cliente. El catálogo no incluye condiciones comerciales aprobadas; puedes solicitar aclaración a un asesor."
            else:
                message = ("Seu perfil corresponde aos critérios de seleção de uma campanha de poupança no corte dos dados." if eligible else
                           "Seu perfil não está selecionado para esta campanha segundo seus critérios; você pode consultar suas informações públicas.")
                message += " A seleção é por conta e não prova inatividade total do cliente. O catálogo não inclui condições comerciais aprovadas; você pode solicitar esclarecimentos a um assessor."
        response = self._result("resolution", message, language, intent)
        response.update(facts=facts, evidence=evidence, tool_events=[dict(tool="read_context", outcome="verified")])
        return response

    def _execute_pending(self, conn, principal, owner, conversation, pending, language, supplied_key):
        if pending.get("source_version") != str(self.store.source_version):
            return self._response("denied", "source_changed", language, pending["action"],
                                  workflow_gate="pending_source_version_changed")
        action = "marketing_optout" if pending["action"] == "marketing_optout" else "request_advisor"
        if pending["customer_id"] != conversation["customer_id"] or not self._allowed(principal, action, pending["customer_id"], confirmed=True):
            return self._response("denied", "denied", language, pending["action"])
        if supplied_key is not None and supplied_key != pending["idempotency_key"]:
            return self._response("denied", "denied", language, pending["action"])
        customer_id, key = pending["customer_id"], pending["idempotency_key"]
        try:
            conn.execute("BEGIN IMMEDIATE")
            if pending["action"] == "marketing_optout":
                self._fault("before_optout_write")
                conn.execute("INSERT INTO consent_overrides VALUES(?,0,?,?) ON CONFLICT(customer_id) DO UPDATE SET accepts_marketing=0,source=excluded.source,updated_at=excluded.updated_at",
                             (customer_id, "confirmed_local_demo_preference", utc_now().isoformat()))
                self._fault("before_optout_readback")
                observed = conn.execute("SELECT accepts_marketing FROM consent_overrides WHERE customer_id=?", (customer_id,)).fetchone()
                if not observed or observed[0] != 0:
                    raise RuntimeError("Consent readback failed")
                conn.execute("INSERT INTO audit(owner,action,outcome,reference,created_at) VALUES(?,?,?,?,?)",
                             (owner, "marketing_optout", "verified", customer_id, utc_now().isoformat()))
                conn.commit()
                return self._response("resolution", "optout_done", language, "marketing_optout",
                                      facts=dict(accepts_marketing=False, override_scope="local_demo"),
                                      tool_events=[dict(tool="marketing_optout", outcome="verified")])
            existing = conn.execute("SELECT * FROM requests WHERE owner=? AND idempotency_key=?", (owner, key)).fetchone()
            if existing is None:
                current = self._context(conn, customer_id)
                if current is None:
                    raise RuntimeError("Unknown customer")
                history = json.loads(conversation["history"])
                assistant_turns = [h for h in history if h.get("role") == "assistant"]
                current_facts = dict(profile_available=current["customer"].get("profile_available", True),
                                     snapshot_only=True, financial_benefit_verified=False)
                if current_facts["profile_available"]:
                    current_facts.update(coherent_account_ids=[a.get("product_id", a.get("account_id")) for a in current["accounts"]],
                                         valid_known_transactions=current["activity"].get("valid_known_transactions", 0),
                                         recent_30d_count=current["activity"].get("recent_30d_count", 0),
                                         activity_quality_caveats=current["activity"].get("quality_caveats", []))
                context = dict(request=pending["reason"], reason=pending["reason"], transcript=history[-8:],
                               verified_facts=[h["facts"] for h in assistant_turns if h.get("facts")] + [current_facts],
                               supporting_evidence=[item for h in assistant_turns for item in h.get("evidence", [])] +
                                   [dict(source_version=str(self.store.source_version), demo_at=self.store.config.get("demo_at"),
                                         historical_states_verified=False, type="authorized_context_read")],
                               attempted_actions=[item for h in assistant_turns for item in h.get("actions", [])],
                               unresolved_questions=[pending["reason"]],
                               unresolved_commercial_terms=any(h.get("intent") == "commercial_terms" for h in assistant_turns),
                               campaign_ids=[c.get("campaign_id") for c in self._campaigns()],
                               source_version=str(self.store.source_version), demo_at=self.store.config.get("demo_at"),
                               historical_states_verified=False, destination="simulation_only")
                self._fault("before_request_write")
                request_id = "REQ-" + secrets.token_hex(12)
                conn.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (request_id, owner, customer_id, key, "pending", "demo_colombia_ahorro", language,
                              canonical(context), str(self.store.source_version), utc_now().isoformat()))
            else:
                if existing["customer_id"] != customer_id:
                    raise RuntimeError("Idempotency scope mismatch")
                request_id = existing["request_id"]
            self._fault("before_request_readback")
            observed = conn.execute("SELECT * FROM requests WHERE request_id=? AND owner=? AND customer_id=?", (request_id, owner, customer_id)).fetchone()
            if observed is None or observed["status"] != "pending":
                raise RuntimeError("Request readback failed")
            conn.execute("INSERT INTO audit(owner,action,outcome,reference,created_at) VALUES(?,?,?,?,?)",
                         (owner, "request_advisor", "verified", request_id, utc_now().isoformat()))
            conn.commit()
            return self._response("handoff_created", "created", language, "advisor_request", request_id=request_id,
                                  facts=dict(request_status="pending", queue="demo_colombia_ahorro", destination="simulation_only"),
                                  tool_events=[dict(tool="request_advisor", outcome="verified", request_id=request_id, idempotent_replay=existing is not None)])
        except (sqlite3.Error, OSError, RuntimeError, ValueError, KeyError):
            conn.rollback()
            return self._response("tool_error", "tool_error", language, pending["action"],
                                  tool_events=[dict(tool=pending["action"], outcome="failed")])

    def get_request(self, token, request_id):
        with self._lock, self._connect() as conn:
            owner, principal = self._principal(conn, token)
            if owner is None or not isinstance(request_id, str):
                return None
            row = conn.execute("SELECT * FROM requests WHERE request_id=? AND owner=?", (request_id, owner)).fetchone()
            if row is None or not self._allowed(principal, "view_customer_context", row["customer_id"]):
                return None
            return dict(request_id=row["request_id"], status=row["status"], queue=row["queue"], language=row["language"],
                        created_at=row["created_at"], context=json.loads(row["context"]), source_version=row["source_version"])

    def get_audience(self, token, limit=100):
        with self._lock, self._connect() as conn:
            _, principal = self._principal(conn, token)
            if not self._allowed(principal, "view_audience"):
                return None
            limit = max(1, min(int(limit), 100))
            # A local opt-out takes immediate precedence over the immutable prepared audience.
            excluded = {row[0] for row in conn.execute("SELECT customer_id FROM consent_overrides WHERE accepts_marketing=0")}
            if hasattr(self.store, "selection"):
                selected = self.store.selection(decision="eligible", limit=limit, excluded_customer_ids=excluded)
                return [dict(campaign_id=row["campaign_id"], customer_id=row["customer_id"]) for row in selected["rows"]]
            audience = self.store.audience(limit=limit)
            if isinstance(audience, list):
                return [row for row in audience if row.get("customer_id") not in excluded]
            if isinstance(audience, dict):
                key = "rows" if "rows" in audience else "audience"
                result = dict(audience)
                if key in result:
                    result[key] = [row for row in result[key] if row.get("customer_id") not in excluded]
                result["local_optout_applied"] = True
                return result
            return []

    def get_profile(self, token):
        """Own data in structured cards; no conversation or supplied ID needed."""
        with self._lock, self._connect() as conn:
            _, principal = self._principal(conn, token)
            customer_id = (principal or {}).get("customer_id")
            if (principal or {}).get("role") != "customer" or not self._allowed(principal, "view_customer_context", customer_id):
                return None
            context = self._context(conn, customer_id)
            if context is None:
                return dict(customer_id=customer_id, profile_available=False, accounts=[], activity=None,
                            campaign_matches=[], analysis_at=self.store.config["demo_at"], source_version=self.store.source_version)
            available = context["customer"].get("profile_available") is not False
            selection = self._informative_response("campaign_info", "es", context)["facts"]["selection"]
            accounts = self._informative_response("account_info", "es", context)["facts"]["accounts"] if available else []
            return dict(customer_id=customer_id, profile_available=available, accounts=accounts,
                        activity=context["activity"] if available else None, campaign_matches=selection,
                        marketing_consent=(context["customer"].get("accepts_marketing") in (1, True, "True")) if available else None,
                        marketing_optout=context["marketing_override"] == 0, analysis_at=self.store.config["demo_at"],
                        source_version=self.store.source_version)

    def request_count(self, token):
        """Authorized local/evaluation accessor; not an HTTP endpoint."""
        with self._lock, self._connect() as conn:
            owner, principal = self._principal(conn, token)
            if owner is None:
                return None
            return conn.execute("SELECT COUNT(*) FROM requests WHERE owner=?", (owner,)).fetchone()[0]
