"""
Script one-time pour créer les sessions Telethon (authentification OTP).
Lancez ce script AVANT app.py pour chaque compte configuré dans .env
"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

load_dotenv()


async def setup_account(api_id: str, api_hash: str, phone: str):
    session_path = f"sessions/{phone.replace('+', '')}"
    os.makedirs("sessions", exist_ok=True)

    client = TelegramClient(session_path, int(api_id), api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print(f"✓ {phone} déjà authentifié")
        await client.disconnect()
        return

    print(f"\nAuthentification de {phone}...")
    await client.send_code_request(phone)
    code = input(f"Code OTP reçu sur {phone}: ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = input("Mot de passe 2FA: ").strip()
        await client.sign_in(password=password)

    if await client.is_user_authorized():
        print(f"✓ {phone} authentifié avec succès, session sauvegardée dans {session_path}.session")
    else:
        print(f"✗ Échec authentification {phone}")

    await client.disconnect()


async def main():
    for i in range(1, 10):
        api_id = os.getenv(f"ACCOUNT_{i}_API_ID", "").strip()
        api_hash = os.getenv(f"ACCOUNT_{i}_API_HASH", "").strip()
        phone = os.getenv(f"ACCOUNT_{i}_PHONE", "").strip()
        if api_id and api_hash and phone:
            await setup_account(api_id, api_hash, phone)

    print("\nSetup terminé. Vous pouvez lancer app.py")


if __name__ == "__main__":
    asyncio.run(main())
