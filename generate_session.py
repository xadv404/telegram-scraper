"""
Script one-time : génère une StringSession à partir de api_id + api_hash + OTP.
La StringSession n'expose pas le numéro de téléphone et remplace l'auth.

Usage :
    python generate_session.py

Copiez la valeur SESSION= dans votre .env sous ACCOUNT_X_SESSION.
"""
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError


async def main():
    print("=== Génération de StringSession Telegram ===\n")
    api_id = input("api_id (https://my.telegram.org) : ").strip()
    api_hash = input("api_hash : ").strip()
    phone = input("Numéro de téléphone (ex: +33612345678) : ").strip()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.connect()

    await client.send_code_request(phone)
    code = input("Code OTP reçu sur Telegram/SMS : ").strip()

    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pw = input("Mot de passe 2FA : ").strip()
        await client.sign_in(password=pw)

    if await client.is_user_authorized():
        session_str = client.session.save()
        print("\n✓ Authentifié !")
        print("\nCopiez cette ligne dans votre .env :")
        print(f"\nACCOUNT_X_SESSION={session_str}\n")
        print("(Le numéro de téléphone n'est plus nécessaire après ça)")
    else:
        print("✗ Échec de l'authentification")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
