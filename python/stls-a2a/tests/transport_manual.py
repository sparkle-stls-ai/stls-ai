from stls_a2a import KeyProviderFactory,KeyProvider,KeyProviderClient,KeyProviderServer,KeySessionProvider
from stls_a2a import A2AMessage,SecureA2ATransport  

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

def main():
    # Crea un'istanza del provider di chiavi (ad esempio, Arqit)
    key_provider_arq:KeyProvider=KeyProviderFactory().get_provider('base')
    logger.info(f'Provider Name -> {key_provider_arq.get_name()}')
    
    # Crea un client provider e avvialo
    client_provider_arq:KeyProviderClient=key_provider_arq.create_provider_client(host='localhost',port=8080,options=None)
    client_provider_arq.start()

    # Crea una sessione di chiavi
    key_session_arq:KeySessionProvider=client_provider_arq.createProviderSession()   
    logger.info(f'session_type={key_session_arq.getSessionType()}') 

    session_key_identifier = asyncio.run(key_session_arq.getSessionKeyIdentifier())
    logger.info(f"session_key_identifier={session_key_identifier}")

    session_key_data=asyncio.run(key_session_arq.getSessionKeyData(session_key_identifier))
    logger.info(f"session_key_data={session_key_data}")

    msg:A2AMessage=A2AMessage(    {
            'jsonrpc': '2.0',
            'method': 'generate',
            'params': {'prompt': 'What is 2+2? I want result no code'},
            'id': 1
        })
    
    transport:SecureA2ATransport=SecureA2ATransport(session_key=session_key_data,
                                                    session_id=session_key_identifier)
    encrypted_payload=transport.encrypt_message(msg)
    logger.info(f"Encrypted message: {encrypted_payload}") 

    decripted_msg=transport.decrypt_message(encrypted_payload)
    logger.info(f"Decrypted message: {decripted_msg}") 



if __name__ == "__main__":
    main()
