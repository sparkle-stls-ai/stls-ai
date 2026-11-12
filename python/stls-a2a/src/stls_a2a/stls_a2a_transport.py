"""
STLS A2A Transport
Extension of the Agent-to-Agent JSON-RPC protocol with symmetric encryption

MIGRATION NOTE: the cryptography (AES-256-GCM + HMAC-SHA256 + HKDF-Expand)
no longer lives here — it has been extracted into stls_core.StlsTransport,
shared with stls-mcp. This module remains only for the A2A-specific
binding: A2AMessage (JSON-RPC) and the SecureA2ATransport/A2ASecureClient/Server
wrappers that work on A2A messages rather than generic bytes/dicts.
"""
import enum
import logging
from typing import Dict, Any, Optional, Callable, Union

from pydantic import BaseModel, ConfigDict

from stls_core import StlsTransport

logger = logging.getLogger(__name__)


class A2AMessage(BaseModel):
    model_config = ConfigDict(exclude_none=True)

    jsonrpc: str = "2.0"
    method: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    id: Optional[Union[int, str]] = None
    """
    JSON-RPC 2.0 (§4.1 spec) allows id to be a string, number, or null —
    not just an integer. a2a-sdk/ADK generate id as a UUID string (e.g.
    "b202b8e2-37e1-43de-8e00-374884280289"), which with id: Optional[int]
    caused a pydantic ValidationError on every encrypted request coming
    from a real a2a-sdk/ADK client (first seen with
    ContextualSecureRemoteA2aAgent, but the bug pre-existed and is in the
    base A2AMessage class: it hits anyone calling /secure-a2a with a
    non-numeric id, not just the new path).
    """
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None


class SecureA2ATransport:
    """
    Secure transport for the A2A protocol.

    Thin wrapper around stls_core.StlsTransport: adds only the
    A2AMessage ↔ dict serialization logic. The cryptography itself
    (AES-256-GCM + HMAC-SHA256 + HKDF-Expand) is entirely delegated
    to stls_core, identical to the one used by stls-mcp.
    """

    def __init__(self, session_key: bytes, session_id: str):
        """
        Initializes the secure transport from a raw session key.

        Low-level constructor: only for callers that legitimately need
        to hold the raw key themselves afterward (e.g. create_pqc_a2a_transport(),
        whose derived key sofia-client-secure persists across process
        restarts via export_session_key() below). Callers going through
        stls-core's resolve_transport()/establish_session() — which
        deliberately never hand back raw key material — should use
        from_transport() instead.

        Args:
            session_key: Shared session key
            session_id: A2A session identifier
        """
        self.session_id = session_id
        self._session_key: Optional[bytes] = session_key
        self._transport = StlsTransport(session_key=session_key, session_id=session_id)

    @classmethod
    def from_transport(cls, transport: StlsTransport, session_id: Optional[str] = None) -> "SecureA2ATransport":
        """
        Builds a SecureA2ATransport from an already-established StlsTransport
        (e.g. from stls_core.resolve_transport()) — no raw key material
        involved at all on this path. export_session_key() is not
        available on an instance built this way (there is no raw key to
        return).
        """
        self = cls.__new__(cls)
        self.session_id = session_id if session_id is not None else transport.session_id
        self._session_key = None
        self._transport = transport
        return self

    def export_session_key(self) -> bytes:
        """
        Returns the raw session key, for callers with a legitimate need
        to persist it themselves (e.g. sofia-client-secure, which
        reconstructs a transport across process restarts without
        re-handshaking). Not a casual accessor — most callers never need
        the raw key at all; use the transport's own encrypt/decrypt
        instead.

        Raises:
            RuntimeError: if this instance was built via from_transport()
                          (established through resolve_transport()/
                          establish_session(), which never expose the
                          raw key in the first place).
        """
        if self._session_key is None:
            raise RuntimeError(
                "export_session_key() is not available on a SecureA2ATransport "
                "built via from_transport() — it was established through "
                "stls_core.resolve_transport()/establish_session(), which never "
                "hand back raw key material."
            )
        return self._session_key

    def encrypt_message(self, message: A2AMessage) -> Dict[str, str]:
        """
        Encrypts an A2A message.

        Args:
            message: A2A message to encrypt

        Returns:
            Dictionary with the encrypted message and metadata (STLS envelope).
        """
        logger.debug(f'message={message}')
        payload = self._message_to_dict(message)
        return self._transport.encrypt_json(payload)

    def decrypt_message(self, encrypted_data: Dict[str, str]) -> A2AMessage:
        """
        Decrypts an A2A message.

        Args:
            encrypted_data: Received encrypted data (STLS envelope)

        Returns:
            Decrypted A2A message

        Raises:
            ValueError: If integrity verification fails or the
                        session_id doesn't match.
        """
        message_dict = self._transport.decrypt_json(encrypted_data)
        return self._dict_to_message(message_dict)

    def _message_to_dict(self, message: "A2AMessage | dict") -> Dict[str, Any]:
        """Converts an A2A message into a JSON-serializable dictionary."""
        if isinstance(message, dict):
            raw = message
        else:
            raw = {
                'jsonrpc': message.jsonrpc,
                **({'method': message.method} if message.method else {}),
                **({'params': message.params} if message.params is not None else {}),
                **({'id': message.id} if message.id is not None else {}),
                **({'result': message.result} if message.result is not None else {}),
                **({'error': message.error} if message.error is not None else {}),
            }

        # Resolves enum and non-JSON-native types (step unchanged from the original)
        import json as _json
        return _json.loads(
            _json.dumps(raw, default=lambda o: o.value if isinstance(o, enum.Enum) else str(o))
        )

    def _dict_to_message(self, data: Dict[str, Any]) -> A2AMessage:
        """Converts a dictionary into an A2A message"""
        return A2AMessage(
            jsonrpc=data.get('jsonrpc', '2.0'),
            method=data.get('method'),
            params=data.get('params'),
            id=data.get('id'),
            result=data.get('result'),
            error=data.get('error')
        )


class A2ASecureClient:
    """A2A client with support for secure RPC calls"""

    def __init__(self, session_key: bytes, session_id: str,
                 send_callback: Callable[[Dict[str, str]], None]):
        """
        Initializes the secure A2A client

        Args:
            session_key: Session key
            session_id: Session ID
            send_callback: Function for sending encrypted messages
        """
        self.transport = SecureA2ATransport(session_key, session_id)
        self.send_callback = send_callback
        self.request_id = 0
        self.pending_requests: Dict[int, Callable] = {}

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        """
        Makes an RPC call

        Args:
            method: Method name
            params: Optional parameters

        Returns:
            Request ID
        """
        self.request_id += 1
        message = A2AMessage(
            jsonrpc="2.0",
            method=method,
            params=params,
            id=self.request_id
        )

        encrypted = self.transport.encrypt_message(message)
        self.send_callback(encrypted)

        return self.request_id

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None):
        """
        Sends a notification (no response expected)

        Args:
            method: Method name
            params: Optional parameters
        """
        message = A2AMessage(
            jsonrpc="2.0",
            method=method,
            params=params
        )

        encrypted = self.transport.encrypt_message(message)
        self.send_callback(encrypted)

    def handle_response(self, encrypted_data: Dict[str, str]) -> A2AMessage:
        """
        Handles an encrypted response

        Args:
            encrypted_data: Received encrypted data

        Returns:
            Decrypted message
        """
        return self.transport.decrypt_message(encrypted_data)


class A2ASecureServer:
    """A2A server with support for handling secure RPCs"""

    def __init__(self, session_key: bytes, session_id: str,
                 send_callback: Callable[[Dict[str, str]], None]):
        """
        Initializes the secure A2A server

        Args:
            session_key: Session key
            session_id: Session ID
            send_callback: Function for sending encrypted messages
        """
        self.transport = SecureA2ATransport(session_key, session_id)
        self.send_callback = send_callback
        self.methods: Dict[str, Callable] = {}

    def register_method(self, name: str, handler: Callable):
        """
        Registers an RPC method

        Args:
            name: Method name
            handler: Function that handles the method
        """
        self.methods[name] = handler

    def handle_request(self, encrypted_data: Dict[str, str]):
        """
        Handles an encrypted request

        Args:
            encrypted_data: Received encrypted data
        """
        try:
            message = self.transport.decrypt_message(encrypted_data)

            # Handle notifications
            if message.id is None:
                if message.method in self.methods:
                    self.methods[message.method](message.params or {})
                return

            # Handle calls
            if message.method not in self.methods:
                response = A2AMessage(
                    jsonrpc="2.0",
                    error={
                        'code': -32601,
                        'message': 'Method not found'
                    },
                    id=message.id
                )
            else:
                try:
                    result = self.methods[message.method](message.params or {})
                    response = A2AMessage(
                        jsonrpc="2.0",
                        result=result,
                        id=message.id
                    )
                except Exception as e:
                    response = A2AMessage(
                        jsonrpc="2.0",
                        error={
                            'code': -32603,
                            'message': str(e)
                        },
                        id=message.id
                    )

            encrypted_response = self.transport.encrypt_message(response)
            self.send_callback(encrypted_response)

        except Exception as e:
            logger.error(f"Error handling request: {e}", exc_info=True)