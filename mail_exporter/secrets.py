from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
from typing import Any


class SecretError(RuntimeError):
    pass


class InvalidAccountPassword(SecretError):
    pass


class SecretCipher:
    """
    Encrypts the IMAP password with a per-account password.

    Encryption is delegated to Ubuntu's OpenSSL using AES-256-CBC and PBKDF2.
    A separate PBKDF2-derived HMAC key authenticates the whole ciphertext
    before decryption.
    """

    VERSION = 2
    ITERATIONS = 600_000
    OPENSSL_CIPHER = "aes-256-cbc"
    AUTHENTICATED_PREFIXES = {
        1: b"gmail-header-exporter:v1:",
        2: b"imap-exporter:v2:",
    }

    def __init__(self) -> None:
        self.openssl = shutil.which("openssl")
        if not self.openssl:
            raise SecretError(
                "O OpenSSL não foi encontrado. Ele faz parte da instalação "
                "padrão do Ubuntu e é necessário para proteger as credenciais."
            )

    @staticmethod
    def validate_account_password(password: str) -> None:
        if len(password) < 8:
            raise SecretError("A senha local da conta deve ter pelo menos 8 caracteres.")
        if len(password) > 1024:
            raise SecretError("A senha local da conta é longa demais.")

    def encrypt(self, imap_password: str, account_password: str) -> str:
        self.validate_account_password(account_password)
        if not imap_password:
            raise SecretError("A senha IMAP não pode ficar vazia.")
        ciphertext = self._openssl(
            imap_password.encode("utf-8"), account_password, decrypt=False
        )
        mac_salt = os.urandom(16)
        mac_key = hashlib.pbkdf2_hmac(
            "sha256",
            account_password.encode("utf-8"),
            mac_salt,
            self.ITERATIONS,
            dklen=32,
        )
        authenticated = self.AUTHENTICATED_PREFIXES[self.VERSION] + ciphertext
        tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
        payload: dict[str, Any] = {
            "version": self.VERSION,
            "cipher": self.OPENSSL_CIPHER,
            "iterations": self.ITERATIONS,
            "mac_salt": base64.b64encode(mac_salt).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    def decrypt(self, payload_text: str, account_password: str) -> str:
        try:
            payload = json.loads(payload_text)
            version = int(payload["version"])
            if version not in self.AUTHENTICATED_PREFIXES:
                raise SecretError("Versão de credencial criptografada não suportada.")
            iterations = int(payload["iterations"])
            if iterations != self.ITERATIONS:
                raise SecretError("Parâmetros da credencial criptografada são inválidos.")
            if payload.get("cipher") != self.OPENSSL_CIPHER:
                raise SecretError("Algoritmo da credencial criptografada não suportado.")
            mac_salt = base64.b64decode(payload["mac_salt"], validate=True)
            ciphertext = base64.b64decode(payload["ciphertext"], validate=True)
            expected_tag = base64.b64decode(payload["tag"], validate=True)
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SecretError("A credencial criptografada está danificada.") from exc

        mac_key = hashlib.pbkdf2_hmac(
            "sha256",
            account_password.encode("utf-8"),
            mac_salt,
            iterations,
            dklen=32,
        )
        authenticated = self.AUTHENTICATED_PREFIXES[version] + ciphertext
        actual_tag = hmac.new(mac_key, authenticated, hashlib.sha256).digest()
        if not hmac.compare_digest(actual_tag, expected_tag):
            raise InvalidAccountPassword("Senha local da conta incorreta.")
        plaintext = self._openssl(ciphertext, account_password, decrypt=True)
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretError("A credencial descriptografada é inválida.") from exc

    def change_password(
        self, payload_text: str, old_password: str, new_password: str
    ) -> str:
        plaintext = self.decrypt(payload_text, old_password)
        return self.encrypt(plaintext, new_password)

    def _openssl(self, data: bytes, password: str, decrypt: bool) -> bytes:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, password.encode("utf-8") + b"\n")
        finally:
            os.close(write_fd)
        command = [
            self.openssl,
            "enc",
            f"-{self.OPENSSL_CIPHER}",
            "-d" if decrypt else "-e",
            "-pbkdf2",
            "-iter",
            str(self.ITERATIONS),
            "-md",
            "sha256",
            "-salt",
            "-pass",
            f"fd:{read_fd}",
        ]
        try:
            completed = subprocess.run(
                command,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(read_fd,),
                check=False,
            )
        finally:
            os.close(read_fd)
        if completed.returncode != 0:
            if decrypt:
                raise InvalidAccountPassword(
                    "Não foi possível abrir a credencial com essa senha local."
                )
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise SecretError(f"Falha ao proteger a credencial: {detail}")
        return completed.stdout
