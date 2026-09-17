"""RFC 3161 trusted timestamps.

Only a SHA-256 digest leaves the machine, so private evidence stays private while an
independent Time Stamping Authority attests when that digest existed. Verification needs
nothing of ours: `openssl ts -verify -data <file> -in <file>.tsr -CAfile <tsa-chain.pem>`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Protocol

import httpx
from asn1crypto import algos, tsp

from brand_evidence.core.clock import to_iso
from brand_evidence.core.logging import get_logger
from brand_evidence.sources.base import user_agent

log = get_logger(__name__)


@dataclass(frozen=True)
class TimestampToken:
    tsr: bytes  # the full TimeStampResp, DER-encoded, what `openssl ts -verify` consumes
    gen_time: str  # ISO-8601 UTC from the TSA's own clock
    digest_hex: str
    tsa_url: str
    serial: str
    policy: str


class TimestampAuthority(Protocol):
    url: str

    def stamp(self, data: bytes) -> TimestampToken: ...


class Rfc3161Authority:
    def __init__(self, url: str, client: httpx.Client | None = None) -> None:
        self.url = url
        self.client = client or httpx.Client(timeout=60.0, headers={"User-Agent": user_agent()})

    def stamp(self, data: bytes) -> TimestampToken:
        digest = hashlib.sha256(data).digest()
        nonce = int.from_bytes(os.urandom(8), "big")
        request = tsp.TimeStampReq(
            {
                "version": "v1",
                "message_imprint": tsp.MessageImprint(
                    {
                        "hash_algorithm": algos.DigestAlgorithm({"algorithm": "sha256"}),
                        "hashed_message": digest,
                    }
                ),
                "nonce": nonce,
                "cert_req": True,
            }
        )
        response = self.client.post(
            self.url,
            content=request.dump(),
            headers={
                "Content-Type": "application/timestamp-query",
                "Accept": "application/timestamp-reply",
            },
        )
        response.raise_for_status()
        return parse_response(response.content, digest, nonce, self.url)


def parse_response(raw: bytes, digest: bytes, nonce: int, tsa_url: str) -> TimestampToken:
    resp = tsp.TimeStampResp.load(raw)
    status = resp["status"]["status"].native
    if status not in ("granted", "granted_with_mods"):
        detail = resp["status"]["status_string"].native if resp["status"]["status_string"] else ""
        raise RuntimeError(f"TSA refused: {status} {detail}".strip())
    info = resp["time_stamp_token"]["content"]["encap_content_info"]["content"].parsed
    if info["message_imprint"]["hashed_message"].native != digest:
        raise RuntimeError("TSA response digest does not match the request")
    if info["nonce"].native is not None and info["nonce"].native != nonce:
        raise RuntimeError("TSA response nonce does not match the request")
    return TimestampToken(
        tsr=raw,
        gen_time=to_iso(info["gen_time"].native),
        digest_hex=digest.hex(),
        tsa_url=tsa_url,
        serial=str(info["serial_number"].native),
        policy=str(info["policy"].native),
    )
