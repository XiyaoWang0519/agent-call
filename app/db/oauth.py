from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.db.protocols import DatabaseAccess

from app.db.engine import _iso_now
from app.grok_oauth import constants as grok_oauth_constants


class OAuthMixin:
    """SQLite persistence for the optional Grok OAuth authorization server."""

    async def oauth_load_runtime_state(self: DatabaseAccess) -> dict[str, Any] | None:
        return await self.fetch_one("SELECT * FROM oauth_runtime_state WHERE singleton = 1")

    async def oauth_initialize_runtime_state(
        self: DatabaseAccess,
        *,
        storage_sentinel: str,
        owner_hash_fingerprint: str,
        signing_key_fingerprint: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_runtime_state
               (singleton, storage_sentinel, owner_hash_fingerprint, signing_key_fingerprint, updated_at)
               VALUES (1, ?, ?, ?, ?)""",
            (storage_sentinel, owner_hash_fingerprint, signing_key_fingerprint, _iso_now()),
        )

    async def oauth_update_runtime_fingerprints(
        self: DatabaseAccess,
        *,
        owner_hash_fingerprint: str,
        signing_key_fingerprint: str,
    ) -> None:
        await self.execute(
            """UPDATE oauth_runtime_state
               SET owner_hash_fingerprint = ?, signing_key_fingerprint = ?, updated_at = ?
               WHERE singleton = 1""",
            (owner_hash_fingerprint, signing_key_fingerprint, _iso_now()),
        )

    async def oauth_upsert_client(
        self: DatabaseAccess,
        *,
        client_id: str,
        ciphertext: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_clients (client_id, ciphertext, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(client_id) DO UPDATE SET
                 ciphertext = excluded.ciphertext,
                 updated_at = excluded.updated_at""",
            (client_id, ciphertext, _iso_now(), _iso_now()),
        )

    async def oauth_get_client(self: DatabaseAccess, client_id: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            "SELECT client_id, ciphertext FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        )

    async def oauth_insert_client_with_quota(
        self: DatabaseAccess,
        *,
        client_id: str,
        ciphertext: str,
        max_clients: int,
        unused_before: str,
        now: str,
    ) -> bool:
        """Insert a DCR client, evicting unused rows so the table cannot grow unbound.

        Unused means the client has no unrevoked family that expires after ``now``.
        Clients unused since before ``unused_before`` are always eligible for eviction.
        Returns False without inserting when every remaining client is still in use.
        """
        unused_exists = """
            NOT EXISTS (
                SELECT 1 FROM oauth_token_families f
                WHERE f.client_id = c.client_id
                  AND f.revoked = 0
                  AND f.expires_at > ?
            )
        """
        async with self._immediate_transaction() as conn:
            await conn.execute(
                f"""DELETE FROM oauth_clients
                    WHERE client_id IN (
                        SELECT c.client_id FROM oauth_clients c
                        WHERE c.created_at < ? AND {unused_exists}
                    )""",
                (unused_before, now),
            )
            async with conn.execute("SELECT COUNT(*) FROM oauth_clients") as cursor:
                count_row = await cursor.fetchone()
            count = int(count_row[0]) if count_row else 0
            if count >= max_clients:
                overflow = count - max_clients + 1
                async with conn.execute(
                    f"""SELECT c.client_id FROM oauth_clients c
                        WHERE {unused_exists}
                        ORDER BY c.created_at ASC, c.client_id ASC
                        LIMIT ?""",
                    (now, overflow),
                ) as cursor:
                    evict = [str(row[0]) for row in await cursor.fetchall()]
                if evict:
                    placeholders, params = self._in_clause(evict)
                    await conn.execute(
                        f"DELETE FROM oauth_clients WHERE client_id IN ({placeholders})",
                        params,
                    )
                async with conn.execute("SELECT COUNT(*) FROM oauth_clients") as cursor:
                    count_row = await cursor.fetchone()
                count = int(count_row[0]) if count_row else 0
            if count >= max_clients:
                return False
            await conn.execute(
                """INSERT INTO oauth_clients (client_id, ciphertext, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(client_id) DO UPDATE SET
                     ciphertext = excluded.ciphertext,
                     updated_at = excluded.updated_at""",
                (client_id, ciphertext, now, now),
            )
            return True

    async def oauth_create_transaction(
        self: DatabaseAccess,
        *,
        transaction_id: str,
        csrf_hash: str,
        ciphertext: str,
        expires_at: str,
        client_id: str = "",
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_auth_transactions
               (transaction_id, client_id, csrf_hash, ciphertext, expires_at, consumed, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (transaction_id, client_id, csrf_hash, ciphertext, expires_at, _iso_now()),
        )

    async def oauth_create_transaction_with_quota(
        self: DatabaseAccess,
        *,
        transaction_id: str,
        client_id: str,
        csrf_hash: str,
        ciphertext: str,
        expires_at: str,
        max_transactions: int,
        max_per_client: int,
        now: str,
    ) -> bool:
        """Insert an authorization transaction after purging expired or consumed rows.

        Returns False without inserting when the global or per-client outstanding
        count is already at the configured bound.
        """
        async with self._immediate_transaction() as conn:
            await conn.execute(
                "DELETE FROM oauth_auth_transactions WHERE expires_at < ? OR consumed = 1",
                (now,),
            )
            async with conn.execute("SELECT COUNT(*) FROM oauth_auth_transactions") as cursor:
                count_row = await cursor.fetchone()
            if int(count_row[0] if count_row else 0) >= max_transactions:
                return False
            async with conn.execute(
                "SELECT COUNT(*) FROM oauth_auth_transactions WHERE client_id = ?",
                (client_id,),
            ) as cursor:
                client_row = await cursor.fetchone()
            if int(client_row[0] if client_row else 0) >= max_per_client:
                return False
            await conn.execute(
                """INSERT INTO oauth_auth_transactions
                   (transaction_id, client_id, csrf_hash, ciphertext, expires_at,
                    consumed, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (transaction_id, client_id, csrf_hash, ciphertext, expires_at, now),
            )
            return True

    async def oauth_get_transaction(
        self: DatabaseAccess, transaction_id: str
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT transaction_id, csrf_hash, ciphertext, expires_at, consumed, created_at
               FROM oauth_auth_transactions WHERE transaction_id = ?""",
            (transaction_id,),
        )

    async def oauth_consume_transaction(self: DatabaseAccess, transaction_id: str) -> bool:
        return await self._execute_cas(
            """UPDATE oauth_auth_transactions
               SET consumed = 1
               WHERE transaction_id = ? AND consumed = 0""",
            (transaction_id,),
        )

    async def oauth_insert_authorization_code(
        self: DatabaseAccess,
        *,
        code_hash: str,
        client_id: str,
        ciphertext: str,
        expires_at: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_authorization_codes
               (code_hash, client_id, ciphertext, expires_at, consumed, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (code_hash, client_id, ciphertext, expires_at, _iso_now()),
        )

    async def oauth_get_authorization_code(
        self: DatabaseAccess, code_hash: str
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT code_hash, client_id, ciphertext, expires_at, consumed
               FROM oauth_authorization_codes WHERE code_hash = ?""",
            (code_hash,),
        )

    async def oauth_consume_authorization_code(self: DatabaseAccess, code_hash: str) -> bool:
        return await self._execute_cas(
            """UPDATE oauth_authorization_codes
               SET consumed = 1
               WHERE code_hash = ? AND consumed = 0""",
            (code_hash,),
        )

    async def oauth_insert_family(
        self: DatabaseAccess,
        *,
        family_id: str,
        client_id: str,
        expires_at: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_token_families
               (family_id, client_id, revoked, created_at, expires_at)
               VALUES (?, ?, 0, ?, ?)""",
            (family_id, client_id, _iso_now(), expires_at),
        )

    async def oauth_get_family(self: DatabaseAccess, family_id: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT family_id, client_id, revoked, created_at, expires_at
               FROM oauth_token_families WHERE family_id = ?""",
            (family_id,),
        )

    async def oauth_revoke_family(self: DatabaseAccess, family_id: str) -> None:
        now = _iso_now()
        await self.execute(
            "UPDATE oauth_token_families SET revoked = 1 WHERE family_id = ?",
            (family_id,),
        )
        await self.execute(
            "UPDATE oauth_refresh_tokens SET revoked = 1, consumed = 1 WHERE family_id = ?",
            (family_id,),
        )
        await self.execute(
            "UPDATE oauth_access_jtis SET revoked = 1 WHERE family_id = ?",
            (family_id,),
        )
        await self.oauth_record_audit("family_revoked", extra={"family_id": family_id, "at": now})

    async def oauth_revoke_all_families(self: DatabaseAccess) -> int:
        rows = await self.fetch_all("SELECT family_id FROM oauth_token_families WHERE revoked = 0")
        for row in rows:
            await self.oauth_revoke_family(str(row["family_id"]))
        return len(rows)

    async def oauth_insert_refresh_token(
        self: DatabaseAccess,
        *,
        token_hash: str,
        family_id: str,
        client_id: str,
        ciphertext: str,
        expires_at: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_refresh_tokens
               (token_hash, family_id, client_id, ciphertext, expires_at, consumed, revoked, created_at)
               VALUES (?, ?, ?, ?, ?, 0, 0, ?)""",
            (token_hash, family_id, client_id, ciphertext, expires_at, _iso_now()),
        )

    async def oauth_get_refresh_token(
        self: DatabaseAccess, token_hash: str
    ) -> dict[str, Any] | None:
        return await self.fetch_one(
            """SELECT token_hash, family_id, client_id, ciphertext, expires_at, consumed, revoked
               FROM oauth_refresh_tokens WHERE token_hash = ?""",
            (token_hash,),
        )

    async def oauth_consume_refresh_token(self: DatabaseAccess, token_hash: str) -> bool:
        return await self._execute_cas(
            """UPDATE oauth_refresh_tokens
               SET consumed = 1
               WHERE token_hash = ? AND consumed = 0 AND revoked = 0""",
            (token_hash,),
        )

    async def oauth_insert_access_jti(
        self: DatabaseAccess,
        *,
        jti: str,
        family_id: str,
        expires_at: str,
    ) -> None:
        await self.execute(
            """INSERT INTO oauth_access_jtis (jti, family_id, expires_at, revoked, created_at)
               VALUES (?, ?, ?, 0, ?)""",
            (jti, family_id, expires_at, _iso_now()),
        )

    async def oauth_get_access_jti(self: DatabaseAccess, jti: str) -> dict[str, Any] | None:
        return await self.fetch_one(
            "SELECT jti, family_id, expires_at, revoked FROM oauth_access_jtis WHERE jti = ?",
            (jti,),
        )

    async def oauth_record_audit(
        self: DatabaseAccess,
        event: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Insert an audit row and prune so the table stays within retention and count bounds."""
        payload = json.dumps(extra or {}, separators=(",", ":"), sort_keys=True)
        now = datetime.now(UTC)
        created_at = now.isoformat()
        cutoff = (
            now - timedelta(seconds=grok_oauth_constants.OAUTH_AUDIT_RETENTION_SECONDS)
        ).isoformat()
        max_rows = grok_oauth_constants.OAUTH_AUDIT_MAX_COUNT
        async with self._immediate_transaction() as conn:
            await conn.execute(
                """INSERT INTO oauth_audit (event, metadata_json, created_at)
                   VALUES (?, ?, ?)""",
                (event, payload, created_at),
            )
            await conn.execute(
                "DELETE FROM oauth_audit WHERE created_at < ?",
                (cutoff,),
            )
            async with conn.execute("SELECT COUNT(*) FROM oauth_audit") as cursor:
                count_row = await cursor.fetchone()
            count = int(count_row[0]) if count_row else 0
            overflow = count - max_rows
            if overflow > 0:
                async with conn.execute(
                    """SELECT id FROM oauth_audit
                       ORDER BY created_at ASC, id ASC
                       LIMIT ?""",
                    (overflow,),
                ) as cursor:
                    evict = [row[0] for row in await cursor.fetchall()]
                if evict:
                    placeholders, params = self._in_clause(evict)
                    await conn.execute(
                        f"DELETE FROM oauth_audit WHERE id IN ({placeholders})",
                        params,
                    )

    async def oauth_purge_expired(self: DatabaseAccess, now: str) -> None:
        """Remove expired OAuth rows, including unconsumed refresh tokens and their families.

        Valid durable refresh families (``expires_at >= now``) are left untouched.
        Dependent refresh and access rows for expired families are deleted first so
        foreign-key checks succeed.
        """
        expired_family = "SELECT family_id FROM oauth_token_families WHERE expires_at < ?"
        async with self._immediate_transaction() as conn:
            await conn.execute(
                "DELETE FROM oauth_auth_transactions WHERE expires_at < ? OR consumed = 1",
                (now,),
            )
            await conn.execute(
                "DELETE FROM oauth_authorization_codes WHERE expires_at < ?",
                (now,),
            )
            await conn.execute(
                "DELETE FROM oauth_access_jtis WHERE expires_at < ?",
                (now,),
            )
            await conn.execute(
                "DELETE FROM oauth_refresh_tokens WHERE expires_at < ?",
                (now,),
            )
            await conn.execute(
                f"DELETE FROM oauth_refresh_tokens WHERE family_id IN ({expired_family})",
                (now,),
            )
            await conn.execute(
                f"DELETE FROM oauth_access_jtis WHERE family_id IN ({expired_family})",
                (now,),
            )
            await conn.execute(
                "DELETE FROM oauth_token_families WHERE expires_at < ?",
                (now,),
            )
